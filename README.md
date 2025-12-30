# O1 Adapter

## Overview

Basic Python-based application that acts as an O1 adapter for srsRAN.
In a k8s deployment it's supposed to be running as a sidecar container to the e.g. srsRAN DU to control the config file generation/provisioning and livecycle of the Pod through a shared volume and REST-based API, respectivly.

## Installation

Using `requirements.txt` or on Ubuntu with `sudo apt-get install python3-ncclient python3-flask python3-xmltodict`

## Operation

Upon start the application attempts to connect to a Config datastore over ssh/netconf. If it succeeds an initial configuration file will be genereated using the `running` datastore.

## Manual Execution

Make sure a Netconf server is running an can be reached over `localhost` on port `830` (those defaults can be changed over the command line). Run the app in one console:

`$ ./o1_adapter.py`

The application will listen on all available network interfaces including `localhost`.

In another console use `curl` to check the config healthiness:

`$ curl -i http://localhost:5000/config-healthy`

It should report the config to be healthy.

Make a config modifcation to the `running` datastore and rerun the command. It should now report `Error code 400` config unhealthy. This status can be used to trigger a restart of the DU container application.

To reset the health status send the following command to the server:

`$ curl -H 'Content-Type: application/json' -d '{ "restarted": True}' -X POST http://localhost:5000/restarted`

An example Docker and k8s integration is provided.

## RU controller

The RU controller is a stand-alone application to configure O-RU over Mplane. Taking the VVDN LPDU as example
we can retrieve the current RU config with:
`$ ./ru_controller.py --host=10.12.1.221 -u root -p vvdn -d running --get_config`

We can activate the carrier with:
`$ ./ru_controller.py --host=10.12.1.221 -u root -p vvdn -d running --tx_gain=26.0 --activate_carriers --carrier_state ACTIVE`

Run a full RU configuration with:
`$ ./ru_controller.py --host=10.10.0.97 -u root -p oruadmin -d running --set_full_config --ru_mac_addr=00:a0:0a:01:a4:42 --vlan=127 --du_mac_addr=9c:69:b4:66:cd:48 --iq_bitwidth=9  --compression_type=STATIC --rf_bandwidth_hz=100000000 --dl_arfcn=649980 --dl_freq=3749700000 --tx_gain=39 --ul_arfcn=649980 --ul_freq=3749700000 --carrier_state ACTIVE`

Note: Full configuration has only been verified for a subset of configuration, e.g. with TDD 100MHz, PRACH format B4.