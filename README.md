# Additional Python and C plugins for collectd
Read plugins:
* LIKWID (C)
* Infiniband bandwidth (sum of send and receive) (Python3)
* Lustre read/write bandwidth and Lustre metadata (Python3)

Write Plugins:
* InfluxDB (Python3)

## Installation of Required Tools and C-Plugins
The plugins have been tested with collectd 5.10.0, but should also work with other versions. Make sure that Python3 is available before installing collectd. If you have an existing Python3 installation, it should be sufficient to install influxdb via pip3.

## Python
The Python plugins are written for Python3. Build Python from sources (including InfluxDB support):
~~~~
PYTHON_VERSION=3.X.X

# get Python sources
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

# install InfluxDB module
pip3 install influxdb
~~~~

### Collectd
To use the *AlignRead* and *AlignReadOffset* options in the collectd configuration file, a patch from the patches folder has to be applied. A respective pull request has been opened (https://github.com/collectd/collectd/pull/3327).

If *AlignRead* is set to *true*, the call to read functions is time aligned to a multiple of the read interval, which allows round timestamps or the same timestamps across systems to be recorded. *AlignReadOffset* specifies an offset, in seconds, the call to time-aligned read functions is delayed.

Build collectd from sources (including the AlignRead feature):
~~~~
# get collectd sources
git clone https://github.com/rdietric/collectd.git
git checkout alignread

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

### InfluxDB
Download a package from https://portal.influxdata.com/downloads/ and install it according to the instructions.
Add the InfluxDB module to your Python 3 installation with `pip3 install influxdb`

### Plugins
Only the C plugin(s) have to be build.

Build Likwid plugin:
~~~~
export LIKWID_ROOT=/likwid/install/path
export COLLECTD_ROOT=/collectd/install/path
export COLLECTD_SRC=/collectd/sources/src
export COLLECTD_BUILD_DIR=/collectd/build/dir

cd c; make
~~~~

## Testing

### Singularity Container
The PIKA data collection can be tested in a singularity container (see [singularity folder](singularity))

### Test collectd
For testing purposes collectd can be run in foreground with `-f`:
~~~~
$COLLECTD_INSTALL_PATH/sbin/collectd -f -C $COLLECTD_CONF_FILE
~~~~

There is a sample configuration file for collectd in the top directory of this repo (*pika_collectd.conf*). Before running collectd, paths in this file have to be adapted, e.g. the path to `custom_types.db`, which is needed for the likwid plugin. You should also disable plugins (comment out), where the resources are not available on the system, e.g. lustre and infiniband, if you are working on your own notebook.

### Likwid Permission Requirements
If you use Likwid with perf_event as access mode, you may not have permission to collect metrics. 
If this happens, you can set perf_event_paranoid to 0 (requires root privileges).

`sh -c 'echo 0 >/proc/sys/kernel/perf_event_paranoid'`

See https://www.kernel.org/doc/Documentation/sysctl/kernel.txt
