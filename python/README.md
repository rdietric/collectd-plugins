# Python plugins for collectd
Add the following lines to *collectd.conf* to load the collectd python plugin and its individual modules.

~~~~
<LoadPlugin python>
   Interval 30
   StartRead 30.1
</LoadPlugin>
<Plugin python>
  ModulePath "/home/rdietric/sw/collectd/python_plugin"
  LogTraces true
  Interactive false
    
  # python plugin imports and module settings

</Plugin>
~~~~

## Infiniband
Collects Infiniband send and receive bandwidth. Add the following lines to *collectd.conf*.

~~~~
Import "ib_bw"
<Module ib_bw>
  devices "/sys/class/infiniband/mlx4_0" # default device
  directory "/sys/class/infiniband" # default search path for devices
  recheck_limit 1440 # seconds after which the availability of the device is checked again
</Module>
~~~~

## Lustre
Collects Lustre read and write bandwidth as well as the following metadata: open, close, fsync, create, seek.
Add the following lines to *collectd.conf*.

~~~~
Import "lustre_bw"
<Module lustre_bw>
  #path "/proc/fs/lustre/llite/XXX"
  recheck_limit 1440 # default seconds after which the availability of the Lustre stats file is checked again
</Module>
~~~~

## InfluxDB
Write plugin which sends data to InfluxDB.

~~~~
Import "influx_write"
<Module influx_write>
  host "localhost"
  port 8086
  user "admin"
  pwd "1234"
  batch_size 200   # number of metrics to be sent at once
  cache_size 2000  # maximum number of metrics to be cached
</Module>
~~~~

# Dummy collectd
Enables testing and debugging of collectd python plugins without installation of collectd.
