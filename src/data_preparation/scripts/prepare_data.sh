#!/usr/bin/env bash

if [ -z "$1" ]; then
    echo "No argument supplied"
    exit 1
fi

if ! [ -f "$1" ]; then
    echo "File doesn't exist"
    exit 1
fi

# Get the input's absolute path
input="$(
    cd "$(dirname "$1")"
    pwd -P
)/$(basename "$1")"
echo "Reading a project list from $input"

mkdir -p /usr/data/raw_repos
cd /usr/data/raw_repos

# To clone a dataset based on a .spec file:
#   * Comment the lines below that clone the dataset and create the .spec
#   * Replace with:
#           bash /usr/src/datasetbuilder/scripts/clone_from_spec.sh path-to-spec-file.spec

bash /usr/src/datasetbuilder/scripts/clone_from_spec.sh pldi2020-dataset-sample.spec

cd ..

git clone --depth=1 https://github.com/microsoft/near-duplicate-code-detector.git
mkdir -p ./repo_tokens
python3 ./near-duplicate-code-detector/tokenizers/python/tokenizepythoncorpus.py ./raw_repos/ ./repo_tokens/
echo "In " $PWD
dotnet run --project ./near-duplicate-code-detector/DuplicateCodeDetector/DuplicateCodeDetector.csproj -- --dir="./repo_tokens/" "./corpus_duplicates"

###
# We are now ready to run pytype on our full corpus
##
export SITE_PACKAGES=/usr/local/lib/python3.6/dist-packages
find ./raw_repos/ -mindepth 1 -maxdepth 1 -type d -print0 | xargs -0 -I {} -P$nproc 'bash pytype.sh {}'

readonly SRC_BASE="/usr/src/datasetbuilder/scripts/"
export PYTHONPATH="$SRC_BASE"
mkdir -p graph-dataset
python3 "$SRC_BASE"graph_generator/extract_graphs.py ./raw_repos/ ./corpus_duplicates.json ./graph-dataset $SRC_BASE/../metadata/typingRules.json --debug
mkdir -p graph-dataset-split
python3 "$SRC_BASE"utils/split.py -data-dir ./graph-dataset -out-dir ./graph-dataset-split
