#!/usr/bin/python -tt

import atexit
import logging
import os
import sys
import time
if sys.version_info >= (3, 0):
  # Python 3 imports.
  import urllib.parse as urlparse
  import urllib.request as urlrequest
else:
  # Python 2 imports.
  import urllib as urlparse
  import urllib2 as urlrequest
sys.path.append("/usr/share/fence")

from fencing import fail_usage, run_delay, all_opt, atexit_handler, check_input, process_input, show_docs, fence_action

try:
	sys.path.insert(0, '/usr/lib/fence-agents/bundled/gce')
	import googleapiclient.discovery
except ImportError:
        pass


METADATA_SERVER = 'http://metadata.google.internal/computeMetadata/v1/'
METADATA_HEADERS = {'Metadata-Flavor': 'Google'}

# String added by mbfx
DEFAULT_CREDENTIALS_PATH = '/gcloud_service_account.json'


def translate_status(instance_status):
	"Returns on | off | unknown."
	if instance_status == "RUNNING":
		return "on"
	elif instance_status == "TERMINATED":
		return "off"
	return "unknown"


def get_nodes_list(conn, options):
	result = {}
	try:
		instanceList = conn.instances().list(project=options["--project"], zone=options["--zone"]).execute()
		for instance in instanceList["items"]:
			result[instance["id"]] = (instance["name"], translate_status(instance["status"]))
	except Exception as err:
		fail_usage("Failed: get_nodes_list: {}".format(str(err)))

	return result


def get_power_status(conn, options):
	try:
		instance = conn.instances().get(
				project=options["--project"],
				zone=options["--zone"],
				instance=options["--plug"]).execute()
		return translate_status(instance["status"])
	except Exception as err:
		fail_usage("Failed: get_power_status: {}".format(str(err)))


def wait_for_operation(conn, project, zone, operation):
	while True:
		result = conn.zoneOperations().get(
			project=project,
			zone=zone,
			operation=operation['name']).execute()
		if result['status'] == 'DONE':
			if 'error' in result:
				raise Exception(result['error'])
			return
		time.sleep(1)


def set_power_status(conn, options):
	try:
		if options["--action"] == "off":
			logging.info("Issuing poweroff of %s in zone %s" % (options["--plug"], options["--zone"]))
			operation = conn.instances().stop(
					project=options["--project"],
					zone=options["--zone"],
					instance=options["--plug"]).execute()
			wait_for_operation(conn, options["--project"], options["--zone"], operation)
			logging.info("Poweroff of %s in zone %s complete" % (options["--plug"], options["--zone"]))
		elif options["--action"] == "on":
			logging.info("Issuing poweron of %s in zone %s" % (options["--plug"], options["--zone"]))
			operation = conn.instances().start(
					project=options["--project"],
					zone=options["--zone"],
					instance=options["--plug"]).execute()
			wait_for_operation(conn, options["--project"], options["--zone"], operation)
			logging.info("Poweron of %s in zone %s complete" % (options["--plug"], options["--zone"]))
	except Exception as err:
		fail_usage("Failed: set_power_status: {}".format(str(err)))


def power_cycle(conn, options):
	try:
		logging.info('Issuing reset of %s in zone %s' % (options["--plug"], options["--zone"]))
		operation = conn.instances().reset(
				project=options["--project"],
				zone=options["--zone"],
				instance=options["--plug"]).execute()
		wait_for_operation(conn, options["--project"], options["--zone"], operation)
		logging.info('Reset of %s in zone %s complete' % (options["--plug"], options["--zone"]))
		return True
	except Exception as err:
		logging.error("Failed: power_cycle: {}".format(str(err)))
		return False


def get_instance(conn, project, zone, instance):
	request = conn.instances().get(
			project=project, zone=zone, instance=instance)
	return request.execute()


def get_zone(conn, project, instance):
	fl = 'name="%s"' % instance
	request = conn.instances().aggregatedList(project=project, filter=fl)
	while request is not None:
		response = request.execute()
		zones = response.get('items', {})
		for zone in zones.values():
			for inst in zone.get('instances', []):
				if inst['name'] == instance:
					return inst['zone'].split("/")[-1]
		request = conn.instances().aggregatedList_next(
				previous_request=request, previous_response=response)
	raise Exception("Unable to find instance %s" % (instance))


def get_metadata(metadata_key, params=None, timeout=None):
	"""Performs a GET request with the metadata headers.

	Args:
		metadata_key: string, the metadata to perform a GET request on.
		params: dictionary, the query parameters in the GET request.
		timeout: int, timeout in seconds for metadata requests.

	Returns:
		HTTP response from the GET request.

	Raises:
		urlerror.HTTPError: raises when the GET request fails.
	"""
	timeout = timeout or 60
	metadata_url = os.path.join(METADATA_SERVER, metadata_key)
	params = urlparse.urlencode(params or {})
	url = '%s?%s' % (metadata_url, params)
	request = urlrequest.Request(url, headers=METADATA_HEADERS)
	request_opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))
	return request_opener.open(request, timeout=timeout * 1.1).read()


def define_new_opts():
	all_opt["zone"] = {
		"getopt" : ":",
		"longopt" : "zone",
		"help" : "--zone=[name]                  Zone, e.g. us-central1-b",
		"shortdesc" : "Zone.",
		"required" : "0",
		"order" : 2
	}
	all_opt["project"] = {
		"getopt" : ":",
		"longopt" : "project",
		"help" : "--project=[name]               Project ID",
		"shortdesc" : "Project ID.",
		"required" : "0",
		"order" : 3
	}
	all_opt["stackdriver-logging"] = {
		"getopt" : "",
		"longopt" : "stackdriver-logging",
		"help" : "--stackdriver-logging		Enable Logging to Stackdriver. Using stackdriver logging requires additional libraries (google-cloud-logging).",
		"shortdesc" : "Stackdriver-logging support. Requires additional libraries (google-cloud-logging).",
		"longdesc" : "If enabled IP failover logs will be posted to stackdriver logging. Using stackdriver logging requires additional libraries (google-cloud-logging).",
		"required" : "0",
		"order" : 4
	}


def main():
	conn = None
	sa_file = None

	device_opt = ["port", "no_password", "zone", "project", "stackdriver-logging", "method"]

	atexit.register(atexit_handler)

	define_new_opts()

	all_opt["power_timeout"]["default"] = "60"
	all_opt["method"]["default"] = "cycle"
	all_opt["method"]["help"] = "-m, --method=[method]          Method to fence (onoff|cycle) (Default: cycle)"

	options = check_input(device_opt, process_input(device_opt))

	docs = {}
	docs["shortdesc"] = "Fence agent for GCE (Google Cloud Engine)"
	docs["longdesc"] = "fence_gce is an I/O Fencing agent for GCE (Google Cloud " \
			   "Engine). It uses the googleapiclient library to connect to GCE.\n" \
			   "googleapiclient can be configured with Google SDK CLI or by " \
			   "executing 'gcloud auth application-default login'.\n" \
			   "For instructions see: https://cloud.google.com/compute/docs/tutorials/python-guide"
	docs["vendorurl"] = "http://cloud.google.com"
	show_docs(options, docs)

	run_delay(options)

	# Prepare logging
	if options.get('--verbose') is None:
		logging.getLogger('googleapiclient').setLevel(logging.ERROR)
		logging.getLogger('oauth2client').setLevel(logging.ERROR)
	if options.get('--stackdriver-logging') is not None and options.get('--plug'):
		try:
			import google.cloud.logging.handlers
			client = google.cloud.logging.Client()
			handler = google.cloud.logging.handlers.CloudLoggingHandler(client, name=options['--plug'])
			handler.setLevel(logging.INFO)
			formatter = logging.Formatter('gcp:stonith "%(message)s"')
			handler.setFormatter(formatter)
			root_logger = logging.getLogger()
			if options.get('--verbose') is None:
				root_logger.setLevel(logging.INFO)
			root_logger.addHandler(handler)
		except ImportError:
			logging.error('Couldn\'t import google.cloud.logging, '
				'disabling Stackdriver-logging support')

	# Prepare credentials
	# Block added by mbfx
	if options.get('--credentials') is None:
		if os.environ.get('GOOGLE_APPLICATION_CREDENTIALS') is None:
			sa_file = DEFAULT_CREDENTIALS_PATH
		else:
			sa_file = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
	elif options.get('--credentials') is not None:
		sa_file = options.get('--credentials')

	# Prepare cli
	# Block changed by erlong15 and mbfx
	try:
		from google.oauth2 import service_account
		credentials = None
		scopes = ['https://www.googleapis.com/auth/cloud-platform']
		credentials = service_account.Credentials.from_service_account_file(sa_file, scopes=scopes)
		conn = googleapiclient.discovery.build('compute', 'v1', credentials=credentials)
	except Exception as err:
		fail_usage("Failed: Create GCE compute v1 connection: {}".format(str(err)))

	# Get project and zone
	if not options.get("--project"):
		try:
			options["--project"] = get_metadata('project/project-id')
		except Exception as err:
			fail_usage("Failed retrieving GCE project. Please provide --project option: {}".format(str(err)))

	if not options.get("--zone"):
		try:
			options["--zone"] = get_zone(conn, options['--project'], options['--plug'])
		except Exception as err:
			fail_usage("Failed retrieving GCE zone. Please provide --zone option: {}".format(str(err)))

	# Operate the fencing device
	result = fence_action(conn, options, set_power_status, get_power_status, get_nodes_list, power_cycle)
	sys.exit(result)

if __name__ == "__main__":
	main()
