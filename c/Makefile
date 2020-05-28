# change the following four lines according to your install and source paths
LIKWID_ROOT ?= /usr/local
COLLECTD_ROOT ?= /usr/local/collectd/$(COLLECTD_VERSION)
COLLECTD_SRC ?= /usr/local/sources/collectd-$(COLLECTD_VERSION)/src
COLLECTD_BUILD_DIR ?= $(COLLECTD_SRC)/..

COLLECTD_INC = $(COLLECTD_ROOT)/include/collectd

CC      = gcc
CFLAGS  = -g -Wall -I$(LIKWID_ROOT)/include -I$(COLLECTD_INC) -I$(COLLECTD_SRC) -I$(COLLECTD_SRC)/daemon -I$(COLLECTD_SRC)/utils/common/ -I$(COLLECTD_BUILD_DIR)/src
LDFLAGS = -L$(LIKWID_ROOT)/lib -L$(COLLECTD_ROOT) -lm

EXECUTABLE=test_likwid
OBJECTS=*.o

all: likwid testplugin influxdb
	
likwid:
	$(CC) -DHAVE_CONFIG_H $(CFLAGS) -std=c99 -shared -fpic -o $(COLLECTD_ROOT)/lib/collectd/likwid.so likwid.c $(LDFLAGS) -llikwid

influxdb:
	$(CC) -DHAVE_CONFIG_H $(CFLAGS) -shared -fpic -o $(COLLECTD_ROOT)/lib/collectd/write_influxdb_udp.so write_influxdb_udp.c $(LDFLAGS)

test_likwid:
	$(CC) -DHAVE_CONFIG_H -DTEST_LIWKID $(CFLAGS) -o test_likwid likwid.c $(LDFLAGS) -llikwid

topo: likwid_get_allocation.c
	$(CC) $(CFLAGS) -o topo likwid_get_allocation.c $(LDFLAGS) -llikwid

testplugin:
	$(CC) -DHAVE_CONFIG_H $(CFLAGS) -shared -fpic -o $(COLLECTD_ROOT)/lib/collectd/testplugin.so test_plugin.c

clean:
	rm -f $(EXECUTABLE)
	rm -f $(OBJECTS)
