import os
import random
import time
from abc import ABC, abstractmethod
from collections import namedtuple, defaultdict
from typing import List, Dict, Any, Iterable, Tuple, Optional, Union, Callable, NamedTuple, Iterator

import numpy as np
import tensorflow as tf
from dpu_utils.utils import RichPath, MultiWorkerCallableIterator

from .utils import run_jobs_in_parallel, partition_files_by_size, ignore_type_annotation

ModelTestResult = namedtuple("ModelTestResult", ["ground_truth", "all_predictions"])


NONE_TOKEN = '<NONE>'


def get_data_files_from_directory(data_dir: RichPath, max_num_files: Optional[int]=None) -> List[RichPath]:
    files = data_dir.get_filtered_files_in_dir('*.gz')
    if max_num_files is None:
        return files
    else:
        return sorted(files)[:int(max_num_files)]


def write_to_minibatch(minibatch: Dict[tf.Tensor, Any], placeholder, val) -> None:
    if type(val) is int:
      minibatch[placeholder] = val
    elif len(val) == 0:
        ph_shape = placeholder.shape.as_list()
        ph_shape[0] = 0
        minibatch[placeholder] = np.empty(ph_shape)
    else:
        minibatch[placeholder] = np.array(val)


def read_data_chunks(data_chunk_paths: Iterable[RichPath], shuffle_chunks: bool=False, max_queue_size: int=1, num_workers: int=0) \
        -> Iterable[List[Dict[str, Any]]]:
    if shuffle_chunks:
        data_chunk_paths = list(data_chunk_paths)
        np.random.shuffle(data_chunk_paths)
    if num_workers <= 0:
        for data_chunk_path in data_chunk_paths:
            yield data_chunk_path.read_by_file_suffix()
    else:
        def read_chunk(data_chunk_path: RichPath):
            return data_chunk_path.read_by_file_suffix()
        yield from MultiWorkerCallableIterator(argument_iterator=[(data_chunk_path,) for data_chunk_path in data_chunk_paths],
                                               worker_callable=read_chunk,
                                               max_queue_size=max_queue_size,
                                               num_workers=num_workers,
                                               use_threads=True)


class Model(ABC):
    @staticmethod
    @abstractmethod
    def get_default_hyperparameters() -> Dict[str, Any]:
        return {
                'optimizer': 'Adam',
                'seed': 0,
                'dropout_keep_rate': 0.9,
                'learning_rate': 0.00025,
                'learning_rate_decay': 0.98,
                'momentum': 0.85,
                'gradient_clip': 1,
                'max_epochs': 500,
                'patience': 10,
               }

    def __init__(self, hyperparameters: Dict[str, Any], run_name: Optional[str]=None, model_save_dir: Optional[str]=None, log_save_dir: Optional[str]=None):
        self.hyperparameters = hyperparameters  # type: Dict[str, Any]
        self.__metadata = {}  # type: Dict[str, Any]
        self.__parameters = {}  # type: Dict[str, tf.Tensor]
        self.__placeholders = {}  # type: Dict[str, tf.Tensor]
        self.__ops = {}  # type: Dict[str, tf.Tensor]
        self._decoder_model = None
        if run_name is None:
            run_name = type(self).__name__
        self.__run_name = run_name

        self.__model_save_dir = model_save_dir or "."
        self.__log_save_dir = log_save_dir or "."

        config = tf.compat.v1.ConfigProto()
        config.gpu_options.allow_growth = True
        if "gpu_device_id" in self.hyperparameters:
            config.gpu_options.visible_device_list = str(self.hyperparameters["gpu_device_id"])

        graph = tf.Graph()
        self.__sess = tf.compat.v1.Session(graph=graph, config=config)

    @property
    def metadata(self):
        return self.__metadata

    @property
    def parameters(self):
        return self.__parameters

    @property
    def placeholders(self):
        return self.__placeholders

    @property
    def ops(self):
        return self.__ops

    @property
    def sess(self):
        return self.__sess

    @property
    def run_name(self):
        return self.__run_name

    def save(self, path: RichPath) -> None:
        unique_vars = set(var.ref() for var in self.__sess.graph.get_collection(tf.compat.v1.GraphKeys.GLOBAL_VARIABLES))
        variables_to_save = list(var.deref() for var in unique_vars)
        weights_to_save = self.__sess.run(variables_to_save)
        weights_to_save = {var.name: value
                           for (var, value) in zip(variables_to_save, weights_to_save)}

        data_to_save = {
                         "model_type": type(self).__name__,
                         "hyperparameters": self.hyperparameters,
                         "metadata": self.__metadata,
                         "weights": weights_to_save,
                         "run_name": self.__run_name,
                       }

        path.save_as_compressed_file(data_to_save)

    def make_model(self, is_train: bool):
        with self.__sess.graph.as_default():
            random.seed(self.hyperparameters['seed'])
            np.random.seed(self.hyperparameters['seed'])
            tf.compat.v1.set_random_seed(self.hyperparameters['seed'])

            self._make_parameters()
            self._make_placeholders(is_train=is_train)
            self._make_model(is_train=is_train)
            if is_train:
                self._make_training_step()

    def _make_training_step(self) -> None:
        """
        Constructs self.ops['train_step'] from self.ops['loss'] and hyperparameters.
        """
        # Calculate and clip gradients
        trainable_vars = tf.compat.v1.trainable_variables()
        gradients = tf.compat.v1.gradients(self.ops['loss'], trainable_vars)
        clipped_gradients, _ = tf.compat.v1.clip_by_global_norm(gradients, self.hyperparameters['gradient_clip'])

        optimizer_name = self.hyperparameters['optimizer'].lower()
        if optimizer_name == 'sgd':
            optimizer = tf.compat.v1.train.GradientDescentOptimizer(learning_rate=self.hyperparameters['learning_rate'])
        elif optimizer_name == 'rmsprop':
            optimizer = tf.compat.v1.train.RMSPropOptimizer(learning_rate=self.hyperparameters['learning_rate'],
                                                  decay=self.hyperparameters['learning_rate_decay'],
                                                  momentum=self.hyperparameters['momentum'])
        elif optimizer_name == 'adam':
            optimizer = tf.compat.v1.train.AdamOptimizer(learning_rate=self.hyperparameters['learning_rate'])
        else:
            raise Exception('Unknown optimizer "%s".' % (self.hyperparameters['optimizer']))

        self.ops['train_step'] = optimizer.apply_gradients(zip(clipped_gradients, trainable_vars))

    @abstractmethod
    def _make_parameters(self) -> None:
        pass

    @abstractmethod
    def _make_placeholders(self, is_train: bool) -> None:
        self.__placeholders['batch_size'] = tf.compat.v1.placeholder(tf.int32, shape=(), name="batch_size")
        self.__placeholders['dropout_keep_rate'] = tf.compat.v1.placeholder(tf.float32, shape=(), name='dropout_keep_rate')

    @abstractmethod
    def _make_model(self, is_train: bool=True) -> None:
        pass

    @staticmethod
    @abstractmethod
    def _init_metadata(hyperparameters: Dict[str, Any], raw_metadata: Dict[str, Any]) -> None:
        """
        Called to initialise the metadata before looking at actual data (i.e., set up Counters, lists, sets, ...)
        :param raw_metadata: A dictionary that will be used to collect the raw metadata (token counts, ...).
        """
        pass

    @staticmethod
    @abstractmethod
    def _load_metadata_from_sample(hyperparameters: Dict[str, Any], raw_sample: Dict[str, Any], raw_metadata: Dict[str, Any]) -> None:
        """
        Called to load metadata from a single sample.
        :param raw_sample: Raw data obtained from the JSON data file.
        :param raw_metadata: A dictionary that will be used to collect the raw metadata (token counts, ...).
        """
        pass

    @abstractmethod
    def _finalise_metadata(self, raw_metadata_list: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Called to finalise the metadata after looking at actual data (i.e., compute vocabularies, ...)
        :param raw_metadata_list: List of dictionaries used to collect the raw metadata (token counts, ...) (one per file).
        :return Finalised metadata (vocabs, ...)
        """
        return {}

    def load_metadata(self, data_dir: RichPath, max_num_files: Optional[int]=None) -> None:
        raw_metadata_list = []

        def metadata_parser_fn(_, file_path: RichPath) -> Iterable[Dict[str, Any]]:
            raw_metadata = {}
            type(self)._init_metadata(self.hyperparameters, raw_metadata)
            for raw_sample in file_path.read_by_file_suffix():
                type(self)._load_metadata_from_sample(self.hyperparameters, raw_sample=raw_sample, raw_metadata=raw_metadata)
            yield raw_metadata

        def received_result_callback(raw_metadata):
            raw_metadata_list.append(raw_metadata)

        def finished_callback():
            pass

        if True:   # Temporarily disable parallelization when needed, by switching this.
            run_jobs_in_parallel(get_data_files_from_directory(data_dir, max_num_files),
                                 metadata_parser_fn,
                                 received_result_callback,
                                 finished_callback)
        else:
            for file in get_data_files_from_directory(data_dir, max_num_files):
                raw_metadata_list.extend(metadata_parser_fn(None, file))

        self.__metadata = self._finalise_metadata(raw_metadata_list)

    def load_existing_metadata(self, metadata_path: RichPath):
        saved_data = metadata_path.read_by_file_suffix()

        hyper_names = set(self.hyperparameters.keys())
        hyper_names.update(saved_data['hyperparameters'].keys())
        if 'cg_node_type_vocab_size' in saved_data['hyperparameters']:
            self.hyperparameters['cg_node_type_vocab_size'] = saved_data['hyperparameters']['cg_node_type_vocab_size']  # TODO: Should not be needed
        for hyper_name in hyper_names:
            if hyper_name in ['run_id']:
                continue  # these are supposed to change
            old_hyper_value = saved_data['hyperparameters'].get(hyper_name)
            new_hyper_value = self.hyperparameters.get(hyper_name)
            if old_hyper_value != new_hyper_value:
                self.train_log("I: Hyperparameter %s now has value '%s' but was '%s' when tensorising data."
                               % (hyper_name, new_hyper_value, old_hyper_value))
        self.__metadata = saved_data['metadata']

    @staticmethod
    @abstractmethod
    def _load_data_from_sample(hyperparameters: Dict[str, Any],
                               metadata: Dict[str, Any],
                               raw_sample: Dict[str, Any],
                               result_holder: Dict[str, Any],
                               is_train: bool=True) -> bool:
        """
        Called to convert a raw sample into the internal format, allowing for preprocessing.
        Result will eventually be fed again into the split_data_into_minibatches pipeline.

        Args:
            hyperparameters: Hyperparameters used to load data.
            metadata: Computed metadata (e.g. vocabularies).
            raw_sample: Raw data obtained from the JSON data file.
            result_holder: Dictionary used to hold the prepared data.
            is_train: Flag marking if we are handling training data or not.

        Returns:
            Flag indicating if the example should be used (True) or dropped (False)
        """
        return True

    @classmethod
    def make_data_file_parser(cls,
                              hyperparameters: Dict[str, Any],
                              metadata: Dict[str, Any],
                              for_test: bool,
                              add_raw_data: bool = False) \
            -> Callable[[Any, Tuple[List[RichPath], RichPath]], Iterable[Tuple[int, int]]]:
        def data_file_parser(_, job_description: Tuple[List[RichPath], RichPath]) -> Iterable[Tuple[int, int]]:
            (file_paths, target_path) = job_description
            num_all_samples = 0
            num_used_samples = 0
            result_data = []
            for file_path in file_paths:
                for raw_sample in file_path.read_by_file_suffix():
                    sample = dict()
                    sample['Provenance'] = raw_sample['filename']
                    use_example = cls._load_data_from_sample(hyperparameters, metadata, raw_sample=raw_sample,
                                                                     result_holder=sample, is_train=not for_test)
                    if add_raw_data:
                        sample['raw_data'] = raw_sample
                    num_all_samples += 1
                    if use_example:
                        num_used_samples += 1
                        result_data.append(sample)
            target_path.save_as_compressed_file(result_data)
            yield num_all_samples, num_used_samples

        return data_file_parser

    def tensorise_data_in_dir(self,
                              input_data_dir: RichPath,
                              output_dir: RichPath,
                              for_test: bool,
                              max_num_files: Optional[int]=None,
                              add_raw_data: bool=False,
                              return_num_original_samples: bool = False) \
            -> Union[List[RichPath], Tuple[List[RichPath], int]]:
        """
        Tensorises data in directory by sample-by-sample, generating "chunk" files of
        lists of tensorised samples that are then consumed in the split_data_into_minibatches
        pipeline to construct minibatches.

        Args:
            input_data_dir: Where to load the raw data from (should come from the extraction pipeline)
            output_dir: Where to store the data to.
            for_test: Flag indicating if the data is to be used for testing (which required additional tensorisation steps)
            max_num_files: Maximal number of files to load data from.
            add_raw_data: Flag indicating that the original data should be added to the tensorised data.
            return_num_original_samples: Flag indicating that the return value should contain the
             number of samples we tried to load, including those that we discarded (e.g., because
             they were too big)

        Return:
            List of paths to the generated chunk files, or tuple of that list and the number
            of samples loaded (iff return_num_original_samples was set)
        """
        data_files = get_data_files_from_directory(input_data_dir, max_num_files)
        tensorisation_argument_tuples = []
        chunk_paths = []
        for (partition_idx, raw_graph_file_partition) in enumerate(partition_files_by_size(data_files, 40 * 1024 * 1024)):
            target_file = output_dir.join("chunk_%04i.pkl.gz" % (partition_idx,))
            tensorisation_argument_tuples.append((raw_graph_file_partition, target_file))
            chunk_paths.append(target_file)

        parsing_result_data = {"num_all_samples": 0, "num_used_samples": 0}
        data_file_parser_fn = type(self).make_data_file_parser(
                                                    self.hyperparameters,
                                                    self.metadata,
                                                    for_test=for_test,
                                                    add_raw_data=add_raw_data)

        def received_result_callback(result):
            (num_all_samples, num_used_samples) = result
            parsing_result_data['num_all_samples'] += num_all_samples
            parsing_result_data['num_used_samples'] += num_used_samples

        def finished_callback():
            pass

        if True:   # Disable job parallelization temporarily
            run_jobs_in_parallel(tensorisation_argument_tuples,
                                 data_file_parser_fn,
                                 received_result_callback,
                                 finished_callback)
        else:
            for sample in tensorisation_argument_tuples:
                received_result_callback(data_file_parser_fn(None, sample))


        # Store the metadata we used for this as well, so that we can re-use the results:
        metadata_path = output_dir.join("metadata.pkl.gz")
        metadata_path.save_as_compressed_file({"hyperparameters": self.hyperparameters,
                                               "metadata": self.__metadata,
                                               "num_used_samples": parsing_result_data['num_used_samples'],
                                               "num_all_samples": parsing_result_data['num_all_samples']})

        self.train_log("Tensorised %i (%i before filtering) samples from '%s' into '%s'."
                       % (parsing_result_data['num_used_samples'],
                          parsing_result_data['num_all_samples'],
                          input_data_dir,
                          output_dir))

        if return_num_original_samples:
            return chunk_paths, parsing_result_data['num_all_samples']
        return chunk_paths

    @abstractmethod
    def _init_minibatch(self, batch_data: Dict[str, Any]) -> None:
        """
        Initialise a minibatch that will be constructed.
        :param batch_data: The minibatch data.
        :return:
        """
        batch_data['samples_in_batch'] = 0
        batch_data["batch_finished"] = False

    @abstractmethod
    def _extend_minibatch_by_sample(self, batch_data: Dict[str, Any], sample: Dict[str, Any]) -> bool:
        """
        Extend a minibatch under construction by one sample.
        :param batch_data: The minibatch data.
        :param sample: The sample to add.
        :return True iff the minibatch is full after this sample.
        """
        return True

    @abstractmethod
    def _finalise_minibatch(self, batch_data: Dict[str, Any], is_train: bool) -> Dict[tf.Tensor, Any]:
        """
        Take a collected minibatch and turn it into something that can be fed directly to the constructed model
        :param batch_data: The minibatch data.
        :return: Map from model placeholders to appropriate data structures.
        """
        return {self.__placeholders['dropout_keep_rate']: self.hyperparameters['dropout_keep_rate'] if is_train else 1.0}

    def train_log(self, msg) -> None:
        if 'run_id' in self.hyperparameters:
            log_path = os.path.join(self.__log_save_dir,
                                    "%s_%s.train_log" % (self.__run_name, self.hyperparameters['run_id'],))
            with open(log_path, mode='a', encoding='utf-8') as f:
                f.write(msg + "\n")
        print(msg.encode('ascii', errors='replace').decode())

    def test_log(self, msg) -> None:
        if 'run_id' in self.hyperparameters:
            log_path = os.path.join(self.__log_save_dir,
                                    "%s_%s.test_log" % (self.__run_name, self.hyperparameters['run_id'],))
            with open(log_path, mode='a', encoding='utf-8') as f:
                f.write(msg + "\n")
        print(msg.encode('ascii', errors='replace').decode())

    def __raw_batches_from_chunks_iterator(self, data_chunk_paths: List[RichPath], is_train: bool=False) -> Iterable[Tuple[Dict[str, Any], int, int]]:
        chunk_iterator = read_data_chunks(data_chunk_paths, shuffle_chunks=is_train, num_workers=5, max_queue_size=25)
        ChunkInformation = namedtuple("ChunkInformation", ["data", "sample_idx_list", "samples_used_so_far"])
        open_chunks_info = []
        def open_new_chunk():
            try:
                new_chunk = next(chunk_iterator)
            except StopIteration:
                return
            num_samples_in_chunk = len(new_chunk)
            chunk_sample_idx_list = np.arange(num_samples_in_chunk)
            if is_train:
                np.random.shuffle(chunk_sample_idx_list)
            open_chunks_info.append(ChunkInformation(new_chunk, chunk_sample_idx_list, [0]))

        # Keep a handful of chunks open:
        for _ in range(25 if is_train else 1):
            open_new_chunk()

        cur_chunk_idx = 0
        cur_batch_data = {}  # type: Dict[str, Any]
        self._init_minibatch(cur_batch_data)
        samples_used_so_far = 0
        while len(open_chunks_info) > 0:
            # Read in round-robin fashion from chunks:
            cur_chunk_idx = (cur_chunk_idx + 1) % len(open_chunks_info)
            cur_chunk_info = open_chunks_info[cur_chunk_idx]

            # Skip empty chunks
            if len(cur_chunk_info.sample_idx_list) == 0:
                del (open_chunks_info[cur_chunk_idx])
                continue

            # Get next sample:
            cur_sample = cur_chunk_info.data[cur_chunk_info.sample_idx_list[cur_chunk_info.samples_used_so_far[0]]]
            cur_batch_data['samples_in_batch'] += 1
            cur_chunk_info.samples_used_so_far[0] += 1

            # Check if chunk is done now, and try open a new one:
            if cur_chunk_info.samples_used_so_far[0] >= len(cur_chunk_info.data):
                del(open_chunks_info[cur_chunk_idx])
                open_new_chunk()  # will silently fail if we are out of chunks

            # Add sample to current minibatch. Yield and prepare fresh one if we are full now:
            batch_finished = self._extend_minibatch_by_sample(cur_batch_data, cur_sample)
            if batch_finished:
                samples_used_so_far += cur_batch_data['samples_in_batch']
                yield cur_batch_data, cur_batch_data['samples_in_batch'], samples_used_so_far
                cur_batch_data = {}
                self._init_minibatch(cur_batch_data)

        # Return the last open, incomplete batch if it's non-empty:
        if cur_batch_data['samples_in_batch'] > 0:
            samples_used_so_far += cur_batch_data['samples_in_batch']
            yield cur_batch_data, cur_batch_data['samples_in_batch'], samples_used_so_far

    def _data_to_minibatches(self, data: Union[List[RichPath], Dict[str, Any]], is_train: bool=False) \
            -> Iterable[Tuple[Dict[tf.Tensor, Any], int, int]]:
        if isinstance(data, list):
            raw_batch_iterator = self.__raw_batches_from_chunks_iterator(data, is_train=is_train)
            for (idx, (raw_batch, samples_in_batch, samples_used_so_far)) in enumerate(raw_batch_iterator):
                minibatch = self._finalise_minibatch(raw_batch, is_train)
                minibatch[self.__placeholders['batch_size']] = samples_in_batch
                yield (minibatch, samples_in_batch, samples_used_so_far)
        else:
            batch_data = {}
            self._init_minibatch(batch_data)
            batch_data['samples_in_batch'] = 1
            self._extend_minibatch_by_sample(batch_data, data)
            minibatch = self._finalise_minibatch(batch_data, is_train)
            minibatch[self.__placeholders['batch_size']] = 1
            yield (minibatch, 1, 1)

    def _run_epoch_in_batches(self, data_chunk_paths: Union[List[RichPath], Dict[str, Any]],
                              epoch_name: str, is_train: bool, quiet: bool=False,
                              additional_fetch_dict: Dict[str, tf.Tensor] = None):
        epoch_loss = 0.0
        epoch_fetches = defaultdict(list)
        epoch_start = time.time()
        data_generator = self._data_to_minibatches(data_chunk_paths, is_train=is_train)
        samples_used_so_far = 0
        printed_one_line = False

        for minibatch_counter, (batch_data_dict, samples_in_batch, samples_used_so_far) in enumerate(data_generator):
            if not quiet and (minibatch_counter % 100) == 0:
                print("%s: Batch %5i (has %i samples). Processed %i samples. Loss so far: %.4f.   "
                      % (epoch_name, minibatch_counter, samples_in_batch,
                         samples_used_so_far - samples_in_batch, epoch_loss / max(1, samples_used_so_far),),
                      flush=True,
                      end="\r")
                printed_one_line = True

            ops_to_run = {'loss': self.__ops['loss']}
            if additional_fetch_dict is not None:
                ops_to_run.update(additional_fetch_dict)
            if is_train:
                ops_to_run['train_step'] = self.__ops['train_step']

            op_results = self.__sess.run(ops_to_run, feed_dict=batch_data_dict)
            assert not np.isnan(op_results['loss'])
            epoch_loss += op_results['loss'] * samples_in_batch

            if additional_fetch_dict is not None:
                for key in additional_fetch_dict:
                    epoch_fetches[key].append(op_results[key])

            minibatch_counter += 1
        used_time = time.time() - epoch_start
        if printed_one_line:
            print("\r\x1b[K", end='')
        if not quiet:
            self.train_log("  Epoch %s took %.2fs [processed %s samples/second]"
                       % (epoch_name, used_time, int(samples_used_so_far/used_time)))

        if samples_used_so_far != 0:
            epoch_loss = epoch_loss / samples_used_so_far

        return epoch_loss, {k: np.concatenate(v, axis=0) for k,v in epoch_fetches.items()}

    @property
    def model_save_path(self) -> str:
        return os.path.join(self.__model_save_dir,
                            "%s_%s_model_best.pkl.gz" % (self.__run_name, self.hyperparameters['run_id'],))

    def train(self, train_data: List[RichPath], valid_data: List[RichPath], quiet: bool=False, resume: bool=False) -> RichPath:
        model_path = RichPath.create(self.model_save_path)
        tf.compat.v1.disable_eager_execution()
        with self.__sess.as_default():
            tf.compat.v1.set_random_seed(self.hyperparameters['seed'])

            if resume:
                # Variables should have been restored.
                best_val_loss, _ = self._run_epoch_in_batches(valid_data, "RESUME (valid)", is_train=False, quiet=quiet)
                self.train_log('Validation Loss on Resume: %.6f' % (best_val_loss,))
            else:
                init_op = tf.compat.v1.variables_initializer(self.__sess.graph.get_collection(tf.compat.v1.GraphKeys.GLOBAL_VARIABLES))
                self.__sess.run(init_op)
                self.save(model_path)
                best_val_loss = float("inf")

            no_improvement_counter = 0
            epoch_number = 0
            while (epoch_number < self.hyperparameters['max_epochs']
                   and no_improvement_counter < self.hyperparameters['patience']):
                self.train_log('==== Epoch %i ====' % (epoch_number,))
                train_loss, _ = self._run_epoch_in_batches(train_data, "%i (train)" % (epoch_number,), is_train=True, quiet=quiet)
                self.train_log(' Training Loss: %.6f' % (train_loss,))
                val_loss, _ = self._run_epoch_in_batches(valid_data, "%i (valid)" % (epoch_number,), is_train=False, quiet=quiet)
                self.train_log(' Validation Loss: %.6f' % (val_loss,))

                if val_loss < best_val_loss:
                    best_val_loss = val_loss
                    no_improvement_counter = 0
                    self.save(model_path)
                    self.train_log("  Best result so far -- saving model as '%s'." % (model_path,))
                else:
                    no_improvement_counter += 1
                epoch_number += 1
        return model_path

    @abstractmethod
    def _encode_one_test_sample(self, sample_data_dict: Dict[tf.Tensor, Any]) -> Tuple[tf.Tensor, Optional[tf.Tensor]]:
        """
        Returns:
            Pair of tensors.
            First element encodes the starting point for the expansion (e.g., selected nodes from a graph, or final state of sequence).
            Second (optional) element encodes the representation of the context, which can be used for attention, copying, etc.
        """
        pass

    Annotation = NamedTuple('Annotation', [
        ('provenance', str),
        ('node_id', int),
        ('name', str),
        ('location', Tuple[int, int]),
        ('original_annotation', str),
        ('annotation_type', str),
        ('predicted_annotation_logprob_dist', Dict[str, float])
    ])

    @abstractmethod
    def annotate_single(self, raw_sample: Dict[str, Any], loaded_test_sample: Dict[str, Any], provenance: str) -> Iterator['Model.Annotation']:
        pass

    def annotate(self, test_raw_data_chunk_paths: List[RichPath]) -> Iterator['Model.Annotation']:
        """Return a list of original annotations and predicted annotations"""
        data_chunk_iterator = (r.read_by_file_suffix() for r in test_raw_data_chunk_paths)
        with self.sess.as_default():
            sample_idx = 0
            for raw_data_chunk in data_chunk_iterator:
                for raw_sample in raw_data_chunk:
                    provenance = raw_sample.get('filename', '?')

                    loaded_test_sample = {}
                    use_example = self._load_data_from_sample(self.hyperparameters,
                                                              self.metadata,
                                                              raw_sample=raw_sample,
                                                              result_holder=loaded_test_sample,
                                                              is_train=False)
                    if not use_example:
                        continue

                    sample_idx += 1
                    yield from self.annotate_single(raw_sample, loaded_test_sample, provenance)

    AnnotationRepresentation = NamedTuple('AnnotationRepresentation', [
        ('name', str),
        ('type_annotation', str),
        ('kind', str),
        ('provenance', str),
        ('representation', np.ndarray),
    ])

    def export_representations(self, data_paths: List[RichPath]) -> Iterator['Model.AnnotationRepresentation']:
        def representation_iter():
            data_chunk_iterator = (r.read_by_file_suffix() for r in data_paths)
            with self.sess.as_default():
                for raw_data_chunk in data_chunk_iterator:
                    for raw_sample in raw_data_chunk:
                        loaded_sample = {}
                        use_example = self._load_data_from_sample(self.hyperparameters,
                                                                  self.metadata,
                                                                  raw_sample=raw_sample,
                                                                  result_holder=loaded_sample,
                                                                  is_train=False)
                        if not use_example:
                            continue

                        _, fetches = self._run_epoch_in_batches(
                            loaded_sample, '(exporting)', is_train=False, quiet=True,
                            additional_fetch_dict={'target_representations': self.ops['target_representations']}
                        )
                        target_representations = fetches['target_representations']

                        idx = 0
                        for node_idx, annotation_data in raw_sample['supernodes'].items():
                            if not ignore_type_annotation(annotation_data['annotation']):
                                yield target_representations[idx], annotation_data['annotation'], annotation_data['type'], annotation_data['name'], f'{raw_sample.get("filename", "Unknown")}:{node_idx}'
                            idx += 1


        for representation, annotated_type, annotation_kind, var_name, provenance in representation_iter():
            yield Model.AnnotationRepresentation(
                name=var_name,
                type_annotation=annotated_type,
                kind=annotation_kind,
                provenance=provenance.encode(errors='ignore').decode('ascii', errors='ignore'),
                representation=representation
            )
