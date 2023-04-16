FROM ubuntu:20.04

RUN touch /etc/inside-container
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y make build-essential libssl-dev zlib1g-dev \
 libbz2-dev libreadline-dev libsqlite3-dev wget curl llvm libncurses5-dev\
 libncursesw5-dev xz-utils tk-dev libffi-dev liblzma-dev python-openssl\
 git
RUN wget -q https://packages.microsoft.com/config/ubuntu/20.04/packages-microsoft-prod.deb \
    -O packages-microsoft-prod.deb && dpkg -i packages-microsoft-prod.deb
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y dotnet-sdk-2.1 python3 python3-pip \
    apt-transport-https software-properties-common git

RUN git clone https://github.com/pyenv/pyenv.git ~/.pyenv
ENV PYENV_ROOT="${HOME}/.pyenv"
ENV PATH="${PYENV_ROOT}/shims:${PYENV_ROOT}/bin:${PATH}"

ENV PYTHON_VERSION=3.6.0
RUN ~/.pyenv/bin/pyenv install ${PYTHON_VERSION} && ~/.pyenv/bin/pyenv global ${PYTHON_VERSION}

COPY ./metadata/popularLibs.txt ./metadata/popularLibs.txt

RUN pip3 install --upgrade pip && \
    pip3 install --no-cache-dir docopt numpy matplotlib typed-ast && \
    pip3 install --no-cache-dir -r metadata/popularLibs.txt && \
    pip3 install --no-cache-dir dpu_utils graphviz split

# For debugging
# RUN pip3 install --no-cache-dir docopt numpy matplotlib typed-ast

# For some reason, the module "setuptools" confuses pytype adding a cycle in the
# dep graph. Remove it here.
RUN apt-get remove -y python3-setuptools

# Subsequent commands are placed here to avoid rebuilding the previous steps if
# anything in the files below changes.

WORKDIR /usr/src/datasetbuilder
COPY ./metadata ./metadata
COPY ./scripts ./scripts
COPY ./*.spec ./
