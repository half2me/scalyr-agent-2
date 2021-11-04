#!/usr/bin/env bash
# Copyright 2014-2021 Scalyr Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

set -e

exit 0
yum install -y tar wget perl git rpm-build
yum groupinstall -y 'Development Tools'

# Build and install openssl, since python 3.8+ requires newer version than centos 7 can provide.
wget https://github.com/openssl/openssl/archive/refs/tags/OpenSSL_1_1_1k.tar.gz -O /tmp/openssl-build.tar.gz

mkdir -p /tmp/openssl-build

tar -xf /tmp/openssl-build.tar.gz -C /tmp/openssl-build --strip-components 1

pushd /tmp/openssl-build || exit 1

/tmp/openssl-build/config --prefix="/usr/local" --openssldir="/usr/local" shared

make -j2

make install

pushd /

echo "/usr/local/lib" >> /etc/ld.so.conf.d/local.conf
echo "/usr/local/lib64" >> /etc/ld.so.conf.d/local.conf
ldconfig


# Build and install python
yum install -y git gcc zlib-devel bzip2 bzip2-devel readline-devel sqlite sqlite-devel \
   tk-devel libffi-devel xz-devel

git clone https://github.com/pyenv/pyenv.git ~/.pyenv

PYTHON_CONFIGURE_OPTS=--enable-shared ~/.pyenv/plugins/python-build/bin/python-build 3.8.10 /usr/local

ldconfig

git clone https://github.com/rbenv/rbenv.git ~/.rbenv

git clone https://github.com/rbenv/ruby-build.git ~/.rbenv/plugins/ruby-build

# Build and install ruby and fpm package.
~/.rbenv/plugins/ruby-build/bin/ruby-build 2.7.3 /usr/local

ldconfig

gem install --no-document fpm -v 1.12.0

gem cleanup

yum clean all

rm -rf /tmp/*

rm -rf ~/.pyenv ~/.rbenv
ldconfig

