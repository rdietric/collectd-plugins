# Additional Python and C plugins for collectd
## Read plugins
* LIKWID (C)
* Infiniband send and receive bandwidth (Python)
* Lustre read and write bandwidth as well as metadata (Python)

## Write Plugins
* InfluxDB (Python)

# Installation (Requirements)
To plugins have been developed for collectd 5.9.0. However, they should work with other versions of collectd. Make sure that Python is available before installing collectd. If you have an existing Python 3 installation, it should be sufficient to install influxdb and nvidia-ml-py via pip3.

## Python
The Python plugins are written for Python3. 

Build Python from sources (including InfluxDB and NVML modules):
~~~~
# get Python3 sources
wget https://www.python.org/ftp/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tar.xz
tar xvfJ Python-${PYTHON_VERSION}.tar.xz

# install Python
cd Python-${PYTHON_VERSION}
./configure --prefix=${DEST_INST} --with-ensurepip=install --enable-shared #--enable-optimizations
make -j; make install
cd ..

export PATH=${DEST_INST}/bin:$PATH
export LD_LIBRARY_PATH=${DEST_INST}/lib:${DEST_INST}/lib/python3.7:$LD_LIBRARY_PATH

pip3 install --upgrade pip

# install InfluxDB and NVML modules
pip3 install influxdb
pip3 install nvidia-ml-py # need for the collectd GPU-CUDA (NVML) plugin
~~~~

## Collectd
A fix for the CUDA-GPU (NVML) plugin and the new *StartRead* setting is available with the branch *prope* of https://github.com/rdietric/collectd.git.

If you use a release version of collectd, the *StartRead* setting will not work. 
StartRead defines for each read plugin, when the first read operation should take place. It is given seconds. Values between 0 and 59 define the start second in a minute and values greater or equal 60 the minute in an hour. The fraction (value after the dot) defines the millisecond of a second.

Build collectd from sources:
~~~~
# get collectd sources
git clone https://github.com/rdietric/collectd.git
git checkout prope

# configure collectd build
cd collectd
./build.sh
PYTHON_CONFIG=$PYTHON_ROOT/bin/python3-config ./configure --prefix=${COLLECTD_INST_PATH} --with-cuda=${CUDA_PATH}

# add the path where the nvml library is located, if building on a system without NVIDIA GPU
export LIBRARY_PATH=$PATH_TO_NVML_LIBRARY:$LIBRARY_PATH

# add paths to plugin.h and collectd.h and to nvml.h as configure for the gpu-nvidia plugin is broken
export C_INCLUDE_PATH=$PWD/src:$PWD/src/daemon:$CUDA_PATH/include:$C_INCLUDE_PATH

# build and install collectd
make -j; make install
~~~~

There are open pull request for the StartRead setting and an improvement for the CUDA-GPU (NVML) plugin:
https://github.com/collectd/collectd/pull/3327  
https://github.com/collectd/collectd/pull/3264

## LIKWID
Likwid is available at https://github.com/RRZE-HPC/likwid.git. You can also use a release version and apply a patch in the folder *patches* (if available).

~~~~
# get likwid sources
LIKWID_VERSION=4.3.3
wget https://github.com/RRZE-HPC/likwid/archive/likwid-${LIKWID_VERSION}.tar.gz
tar xfz likwid-${LIKWID_VERSION}.tar.gz
cd likwid-likwid-${LIKWID_VERSION}
patch -p0 < $PATH_TO_PATCH/prope_likwid-${LIKWID_VERSION}_src.patch

# set Likwid install path ($LIKWID_INST_PATH), perf_event as counter source and disable building the access daemon
sed -i "/^PREFIX = .*/ s|.*|PREFIX = $LIKWID_INST_PATH|" config.mk
sed -i "/^ACCESSMODE = .*/ s|.*|ACCESSMODE = perf_event|" config.mk
sed -i "/^BUILDDAEMON = .*/ s|.*|BUILDDAEMON = false|" config.mk
make -j4; make install
cd ..
~~~~

## InfluxDB
Download a package from https://portal.influxdata.com/downloads/ and install it according to the instructions.
Add the InfluxDB module to your Python 3 installation with `pip3 install influxdb`

## Plugins
Only the C plugin(s) have to be build.

Build Likwid plugin:
~~~~
export LIKWID_ROOT=/likwid/install/path
export COLLECTD_ROOT=/collectd/install/path
export COLLECTD_SRC=/collectd/sources/src
export COLLECTD_BUILD_DIR=/collectd/build/dir

cd c; make
~~~~

# Run collectd
For testing purposes collectd can be run in foreground with `-f`:
~~~~
$COLLECTD_INSTALL_PATH/sbin/collectd -f -C $PATH_TO_COLLECTD_CONF/collectd.conf
~~~~

There is a sample configuration file for collectd in the top directory of this repo (*collectd_prope.conf*). Before running collectd, paths in this file have to be adapted, e.g. the path to `custom_types.db`, which is needed for the likwid plugin. You should also disable plugins (comment out), where the resources are not available on the system, e.g. lustre and infiniband, if you are working on your own notebook.
