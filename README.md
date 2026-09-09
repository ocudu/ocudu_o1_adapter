# OCUDU O1 Adapter

## Overview

Python application acting as adapter between CU/DU towards the Service Management and Orchestration (SMO).
The application currently supports:
* Configuration Management (CM),
* Fault Management (FM).

Performance Managment (PM) is currently implemented through a Json-based metrics service exposed over the
Websocket interface of OCUDU.

In a Kubernetes deployment the O1-Adapter is supposed to be running as a sidecar container next to the e.g.
OCUDU DU container to control the config file generation/provisioning and livecycle of the Pod through a shared volume and REST-based API, respectively.

## Installation

Using `requirements.txt` or on Ubuntu with `sudo apt-get install python3-ncclient python3-flask python3-xmltodict python3-websockets python3-deepdiff`

Within a python virtual environment, install the requirements with `pip install -r requirements.txt`

## Operation

Upon start the application attempts to connect to a Config datastore over SSH/netconf. If it succeeds an initial configuration file will be genereated using the `running` datastore.

## Manual Execution

Make sure a Netconf server is running an can be reached over `localhost` on port `830` (those defaults can be changed over the command line). Run the app in one console:

`$ python3 src/o1_adapter`

The application will listen on all available network interfaces including `localhost`.

In another console use `curl` to check the config healthiness:

`$ curl -i http://localhost:5000/config-healthy`

It should report the config to be healthy.

Make a config modifcation to the `running` datastore and rerun the command. It should now report `Error code 400` config unhealthy. This status can be used to trigger a restart of the DU container application.

To reset the health status send the following command to the server:

`$ curl -H 'Content-Type: application/json' -d '{ "restarted": True}' -X POST http://localhost:5000/restarted`

An example Docker and k8s integration is provided.

### Verifying the NETCONF server's SSH host key

The adapter accepts whatever SSH host key the server presents unless `--netconf_hostkey_verify`
is given. With the flag it checks the key against `--netconf_known_hosts`
(default `/etc/netconf-ssh/known_hosts`) and refuses to start if that file is missing:

```
$ python3 src/o1_adapter.py --netconf_hostkey_verify --netconf_known_hosts ./known_hosts
```

The entry has to name the port whenever it is not 22:

```bash
printf '[%s]:%s %s\n' localhost 830 "$(cut -d' ' -f1,2 ./ssh_host_ed25519_key.pub)" > known_hosts
```

Provision the server to match — see *Provision the SSH host key* in the `ocudu_netconf` README,
or `o1.netconfServer.ssh.hostKeySecret` under Helm — otherwise the key changes on every netconf
image rebuild. The key has to be ed25519 or ecdsa: an RSA one is recorded in `known_hosts` as
`ssh-rsa` while the server offers only `rsa-sha2-512`/`rsa-sha2-256`, and ncclient narrows the
transport to the recorded name, so key exchange finds nothing in common. Such a `known_hosts`
file is refused at startup rather than failing later in the handshake.

`--netconf_tls` authenticates the server by certificate instead, and then logs this flag as
ignored.

### Adapter component profiles

The adapter ships templates for the following gNB split components: `gnb.yaml`, `cu.yaml`, `cucp.yaml`, `cuup.yaml` and `du.yaml`. Select one with `--profile`, e.g.:

```
$ python3 src/o1_adapter.py --profile cucp
```

`--profile` defaults to `gnb` and selects `<profile>.yaml` as the rendering template. Pass `--template <file>` to override the template explicitly. The special value `--profile ru` skips YAML rendering entirely and only forwards the raw NETCONF config downstream.

### RU controller within O1 Adapter

With the option `--ru_forward` the O1 Adapter automatically forwards configuration updates from the NETCONF server of the DU to the NETCONF server of the RU. In this case it has to be ensured, that two NETCONF servers are reachable. One represents the NETCONF server on the DU and the other NETCONF server runs on the RU. Then the forwarding can be achieved by the following command:

```
$ python3 src/o1_adapter.py --netconf_host <DU-IP-ADDRESS> --netconf_username <DU-NETCONF-USERNAME> --netconf_password <DU-USER-PASSWORD> --ru_forward --ru_netconf_host <RU-IP-ADDRESS> --ru_netconf_username <RU-NETCONF-USERNAME> --ru_netconf_password <RU-USER-PASSWORD>
```

## O-RU Mplane

The adapter includes an Mplane client for O-RAN WG4 O-RUs, in two layers:

* **Client / CLI**: the `RuConfig` library in `src/ru_config.py` (also
  driven by the `--ru_forward` path) and the stand-alone CLI in
  `src/ru_controller.py`. It provisions the fronthaul (interfaces,
  processing element, endpoints, carriers, links, TDD pattern and
  activation), derives the DU timing windows from the O-RU's
  delay-management data, discovers capabilities through the yang-library,
  keeps the supervision session alive, configures performance measurements
  and reads the synchronization state. `--role` selects the NACM account
  group the client acts as (`sudo`, the default, or `hybrid-odu` per the
  O-RAN WG4 M-plane specification, Table 6.5-1). See
  [docs/mplane-client.md](docs/mplane-client.md).
* **Service** (`--ru_supervise`): a resident supervised Mplane session owned
  by the adapter — reconnecting, notification-driven supervision for the
  process lifetime, optional call-home (`--ru_callhome`, RFC 8071), and the
  session lifecycle surfaced in the shared state, the log and alarms
  1003/1004. `--profile ru` runs it without the DU-facing loops. See
  [docs/mplane-service.md](docs/mplane-service.md).

Development and testing against a simulated O-RU is described in
[docs/mplane-sim-testing.md](docs/mplane-sim-testing.md). `--rpc_log FILE`
(CLI and adapter) records the raw NETCONF conversation.

## RU controller

The RU controller is a stand-alone application to configure an O-RU over Mplane.
Taking an example RU that exposes it's Netconf interface over `10.10.0.100` using `admin/admin` as login credentials for example, we can retrieve the current RU config with:

```
$ ./ru_controller.py --host=10.10.0.100 -u admin -p admin -d running --get_config
```

We can activate the carrier with:

```
$ ./ru_controller.py --host=10.10.0.100 -u admin -p admin -d running --tx_gain=26.0 --activate_carriers --carrier_state ACTIVE
```

Run a full RU configuration with:

```
$ ./ru_controller.py --host=10.10.0.100 -u admin -p admin -d running --set_full_config --ru_mac_addr=00:a0:0a:01:a4:42 --vlan=127 --du_mac_addr=9c:69:b4:66:cd:48 --iq_bitwidth=9  --compression_type=STATIC --rf_bandwidth_hz=100000000 --dl_arfcn=649980 --dl_freq=3749700000 --tx_gain=39 --ul_arfcn=649980 --ul_freq=3749700000 --carrier_state ACTIVE
```

Note: Full configuration has only been verified for a subset of configuration, e.g. with TDD 100MHz, PRACH format B4.

## License

This project is licensed under the BSD 3-Clause Open MPI variant License – see the [LICENSE](./LICENSE) file for details.
