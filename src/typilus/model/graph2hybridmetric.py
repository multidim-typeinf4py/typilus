from typing import List, Dict, Any, Tuple, Optional

import numpy as np
import tensorflow as tf
from dpu_utils.utils import RichPath

from typilus.model.model import write_to_minibatch
from typilus.model.typeclassificationmodel import TypeClassificationModel
from typilus.model.typemetriclearningmodel import TypeMetricLearningModel
from typilus.model.utils import ignore_type_annotation
from .codegraphmodel import CodeGraphModel


class Graph2HybridMetric(CodeGraphModel):
    @staticmethod
    def _init_metadata(hyperparameters: Dict[str, Any], raw_metadata: Dict[str, Any]) -> None:
        super(Graph2HybridMetric, Graph2HybridMetric)._init_metadata(hyperparameters, raw_metadata)
        TypeClassificationModel._init_metadata(hyperparameters, raw_metadata)

    @staticmethod
    def _load_metadata_from_sample(hyperparameters: Dict[str, Any], raw_sample: Dict[str, Any],
                                   raw_metadata: Dict[str, Any]) -> None:
        super(Graph2HybridMetric, Graph2HybridMetric)._load_metadata_from_sample(hyperparameters, raw_sample, raw_metadata)
        TypeClassificationModel._load_metadata_from_sample(hyperparameters, raw_sample, raw_metadata)

    def _finalise_metadata(self, raw_metadata_list: List[Dict[str, Any]]) -> Dict[str, Any]:
        final_metadata = super()._finalise_metadata(raw_metadata_list)
        self.__type_classification._finalise_metadata(raw_metadata_list, final_metadata)
        return final_metadata

    @staticmethod
    def get_default_hyperparameters() -> Dict[str, Any]:
        defaults = CodeGraphModel.get_default_hyperparameters()
        defaults.update({
            'margin': 2,
            'max_type_annotation_vocab_size': 100,
            'strip_type_parameters': True
        })
        return defaults

    def __init__(self, hyperparameters, run_name: Optional[str] = None, model_save_dir: Optional[str] = None, log_save_dir: Optional[str] = None):
        super().__init__(hyperparameters, run_name, model_save_dir, log_save_dir)
        self.__type_metric = TypeMetricLearningModel(self,
                                                     type_representation_size=self.hyperparameters['cg_ggnn_hidden_size'],
                                                     margin=self.hyperparameters['margin'])
        self.__type_classification = TypeClassificationModel(self)

    def _make_placeholders(self, is_train: bool) -> None:
        super()._make_placeholders(is_train)
        self.placeholders['typed_annotation_node_ids'] = tf.placeholder(tf.int32,
                                                                        shape=(
                                                                            None,),
                                                                        name="typed_annotation_node_ids")
        self.__type_metric._make_placeholders(is_train)
        self.__type_classification._make_placeholders(is_train)

    def _make_parameters(self):
        super()._make_parameters()
        representation_size = self.hyperparameters['cg_ggnn_hidden_size']
        self.parameters['metric_to_classification_linear_layer'] = \
            tf.compat.v1.get_variable(name='metric_to_classification_linear_layer',
                            initializer=tf.random_normal_initializer(),
                            shape=[representation_size, representation_size],
                            )
        self.__type_classification._make_parameters(representation_size=representation_size)

    def _make_model(self, is_train: bool = True):
        super()._make_model(is_train)

        # Gather up CG node representations for the nodes we want to type
        target_node_representations_from_cg = tf.gather(params=self.ops['cg_node_representations'],
                                                        indices=self.placeholders['typed_annotation_node_ids'])
        self.ops['target_representations'] = target_node_representations_from_cg

        self.__type_metric._make_model(target_node_representations_from_cg, is_train)

        classification_representation = tf.matmul(target_node_representations_from_cg,
                                            self.parameters['metric_to_classification_linear_layer'])
        classification_representation = tf.nn.dropout(classification_representation,
                                                rate=1-self.placeholders['dropout_keep_rate'])
        self.__type_classification._make_model(classification_representation, is_train)

    @staticmethod
    def _load_data_from_sample(hyperparameters: Dict[str, Any],
                               metadata: Dict[str, Any],
                               raw_sample: Dict[str, Any],
                               result_holder: Dict[str, Any],
                               is_train: bool = True) -> bool:
        keep_sample = super(Graph2HybridMetric, Graph2HybridMetric)._load_data_from_sample(
            hyperparameters, metadata, raw_sample, result_holder, is_train)
        if not keep_sample:
            return False

        target_node_idxs, target_class, target_class_id = [], [], []
        for node_idx, annotation_data in raw_sample['supernodes'].items():
            node_idx = int(node_idx)
            annotation = annotation_data['annotation']
            if is_train and ignore_type_annotation(annotation):
                continue
            target_node_idxs.append(node_idx)
            target_class.append(annotation)
            target_class_id.append(TypeClassificationModel._get_idx_for_type(annotation, metadata, hyperparameters))

        result_holder['target_node_idxs'] = np.array(target_node_idxs, dtype=np.uint16)
        result_holder['target_type'] = target_class
        result_holder['variable_target_class'] = np.array(target_class_id, dtype=np.uint16)
        return len(target_node_idxs) > 0

    def _init_minibatch(self, batch_data: Dict[str, Any]) -> None:
        super()._init_minibatch(batch_data)
        batch_data['batch_target_node_idxs'] = []
        self.__type_metric._init_minibatch(batch_data)
        self.__type_classification._init_minibatch(batch_data)

    def _extend_minibatch_by_sample(self, batch_data: Dict[str, Any], sample: Dict[str, Any]) -> bool:
        batch_finished = super()._extend_minibatch_by_sample(batch_data, sample)
        # The offset has already been updated in our parent...
        original_cg_node_offset = batch_data['cg_node_offset'] - sample['num_nodes']
        batch_data['batch_target_node_idxs'].extend(sample['target_node_idxs'] + original_cg_node_offset)
        self.__type_classification._extend_minibatch_by_sample(batch_data, sample)
        self.__type_metric._extend_minibatch_by_sample(batch_data, sample)
        return batch_finished

    def _finalise_minibatch(self, batch_data: Dict[str, Any], is_train: bool) -> Dict[tf.Tensor, Any]:
        minibatch = super()._finalise_minibatch(batch_data, is_train)
        write_to_minibatch(
            minibatch, self.placeholders['typed_annotation_node_ids'], batch_data['batch_target_node_idxs'])
        self.__type_classification._finalise_minibatch(batch_data, is_train, minibatch)
        self.__type_metric._finalise_minibatch(batch_data, is_train, minibatch)
        return minibatch

    def create_index(self, data_paths: List[RichPath]):
        self.__type_metric.create_index(data_paths, self.metadata)


    # ------- These are the bits that we only need for test-time:
    def _encode_one_test_sample(self, sample_data_dict: Dict[tf.Tensor, Any]) -> Tuple[tf.Tensor, Optional[tf.Tensor]]:
        return (self.sess.run(self.ops['target_representations'],
                              feed_dict=sample_data_dict),
                None)

    def annotate_single(self, raw_sample: Dict[str, Any], loaded_test_sample: Dict[str, Any], provenance: str):
        return self.__type_metric.annotate_single(raw_sample, loaded_test_sample, provenance, self.metadata)
