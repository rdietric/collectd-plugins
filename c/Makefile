# change the following four lines according to your install and source paths
LIKWID_ROOT ?= /usr/local
COLLECTD_ROOT ?= /usr/local/collectd/$(COLLECTD_VERSION)
COLLECTD_SRC ?= /usr/local/sources/collectd-$(COLLECTD_VERSION)/src
COLLECTD_BUILD_DIR ?= $(COLLECTD_SRC)/..

COLLECTD_INC = $(COLLECTD_ROOT)/include/collectd

CC      = gcc
CFLAGS  = -g -I$(LIKWID_ROOT)/include -I$(COLLECTD_INC) -I$(COLLECTD_SRC) -I$(COLLECTD_SRC)/daemon -I$(COLLECTD_SRC)/utils/common/ -I$(COLLECTD_BUILD_DIR)/src
LDFLAGS = -L$(LIKWID_ROOT)/lib -L$(COLLECTD_ROOT) -llikwid

all: cplugin 
	
cplugin:
	$(CC) -DHAVE_CONFIG_H $(CFLAGS) -shared -fpic -o $(COLLECTD_ROOT)/lib/collectd/likwid.so likwid.c $(LDFLAGS)

clean:
	rm -f $(EXECUTABLE)
	rm -f $(OBJECTS)
