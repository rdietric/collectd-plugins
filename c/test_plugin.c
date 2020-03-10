#include <stdlib.h>
#include <stdio.h>
#include <stdint.h>
#include <unistd.h>
#include <stdbool.h>
#include <string.h>

#include "config.h" // sets several defines

// headers required for collectd
#include "collectd.h"
#include "common.h" /* auxiliary functions */
#include "plugin.h" /* plugin_register_*, plugin_dispatch_values */

#define PLUGIN_NAME "testplugin"

static int test_plugin_read_complex(user_data_t *ud) {
  INFO(PLUGIN_NAME ": %s:%d", __FUNCTION__, __LINE__);
}

static int test_plugin_read(void) {  
  INFO(PLUGIN_NAME ": %s:%d", __FUNCTION__, __LINE__);

  return 0;
}

static int test_plugin_init(void)
{
  INFO(PLUGIN_NAME ": %s:%d", __FUNCTION__, __LINE__);

  char *interval_str = getenv("MY_INTERVAL");

  double interval = strtod(interval_str, NULL);
  if(interval == 0.0){
    WARNING("No interval for complex read. Defaulting to 10.0.");
    interval = 10.0;
  }
  else{
    INFO("set interval for complex read to %.2lf.", interval);
  }
  plugin_register_complex_read("testcomplex", "testreadcomplex", test_plugin_read_complex, DOUBLE_TO_CDTIME_T(interval),NULL);
  
  return 0;
}

static int test_plugin_flush(cdtime_t timeout, const char *identifier, user_data_t *usr )
{
  INFO(PLUGIN_NAME ": %s:%d", __FUNCTION__, __LINE__);
  
  return 0;
}

/*! brief Resets the test group counters

Example notification on command line:
echo "PUTNOTIF severity=okay time=$(date +%s) message=resetLikwidCounters" |   socat - UNIX-CLIENT:$HOME/sw/collectd/collectd-unixsock
 */
static int test_plugin_notify(const notification_t *type, user_data_t *usr )
{
  INFO (PLUGIN_NAME ": %s:%d", __FUNCTION__, __LINE__);
  
  return 0;
}

static int test_plugin_finalize( void )
{
  INFO (PLUGIN_NAME ": %s:%d", __FUNCTION__, __LINE__);

  return 0;
}

static const char *config_keys[] =
{
  "verbose"
};
static int config_keys_num = STATIC_ARRAY_SIZE(config_keys);

static int test_plugin_config (const char *key, const char *value)
{
  INFO (PLUGIN_NAME " config: %s := %s", key, value);
  
  return 0;
}

/*
 * This function is called after loading the plugin to register it with collectd.
 */
void module_register(void) {
  plugin_register_config (PLUGIN_NAME, test_plugin_config, config_keys, config_keys_num);
  plugin_register_read(PLUGIN_NAME, test_plugin_read);
  plugin_register_init(PLUGIN_NAME, test_plugin_init);
  plugin_register_shutdown(PLUGIN_NAME, test_plugin_finalize);
  plugin_register_flush(PLUGIN_NAME, test_plugin_flush, /* user data = */ NULL);
  plugin_register_notification(PLUGIN_NAME, test_plugin_notify, /* user data = */ NULL);
  return;
}
