# LtAP Lab Setup 2026-08-07

## Workstation

- OS: Linux Mint 22.1.
- Kernel during setup: `6.8.0-137-generic`.
- LtAP-facing Ethernet interface: `eno1`.
- NetworkManager profile: `LtAP-Lab`.
- Management address: `192.168.103.200/24`.
- LTE1 test source: `192.168.103.201/24`.
- LTE2 test source: `192.168.103.202/24`.
- LtAP gateway: `192.168.103.254`.

`LtAP-Lab` is configured with `ipv4.never-default yes`; Wi-Fi remains the normal
system default route.

## Linux Policy Routing

Route tables:

```text
201 ltap-lte1
202 ltap-lte2
```

Rules:

```text
priority 20100 from 192.168.103.201/32 lookup ltap-lte1
priority 20200 from 192.168.103.202/32 lookup ltap-lte2
```

Verification after cycling only the `LtAP-Lab` profile:

```text
1.1.1.1 from 192.168.103.201 via 192.168.103.254 dev eno1 table ltap-lte1
1.1.1.1 from 192.168.103.202 via 192.168.103.254 dev eno1 table ltap-lte2
```

Persistence is installed via:

- `/etc/iproute2/rt_tables.d/ltap-test.conf`
- `/usr/local/sbin/ltap-test-routing`
- `/etc/NetworkManager/dispatcher.d/90-ltap-test-routing`

Tracked source copies are under `scripts/`.

## Router Verification

- Router: MikroTik `LtAP-2HnD`.
- RouterOS: `7.23.2 (stable)`.
- Router management address: `192.168.103.254/24` on `bridge1`.
- LTE interfaces: `lte1`, `lte2`.
- Routing tables: `to-lte1`, `to-lte2`.
- Mangle policy:
  - `192.168.103.201` -> `to-lte1`.
  - `192.168.103.202` -> `to-lte2`.
- NAT: masquerade via WAN interface list.
- FastTrack: no enabled FastTrack rule found in the test firewall snapshot.

SSH key authentication works with `~/.ssh/ltap_test_ed25519`.

## Current LTE State

- `lte1`: registered; modem model `R11l-LTE7`, firmware `R11l-LTE7_V005`.
- `lte2`: present; modem model `R11l-LTE7`, firmware `R11l-LTE7_V005`; status
  `radio off`, `SIM not inserted`.

This means LTE1 tests can pass now. LTE2 acceptance cannot pass until the
physical SIM/modem state is corrected.

## Public iPerf3 Campaign

- Hostname: `iperf-ams-nl.eranium.net`.
- Pinned IPv4: `217.18.95.142`.
- Allowed ports: `5201-5210`.
- Local collector directory:
  `references/public-iperf-kit/`.

The collector was patched to version `0.3.0`:

- removed `--get-server-output`, because this public server still returns
  receiver-side UDP loss/jitter in normal JSON and the extra output block caused
  false non-zero exits under loaded LTE latency;
- writes `events.jsonl` for LTE status, primary-band, CA, cell, and low-SINR
  state changes;
- appends `summary.csv` only for validated runs.

## Smoke Results

Validated LTE1 setup smoke:

- Tag: `setup_smoke_lte1`.
- Profile: UDP `4M`, 1200-byte payload, 30 seconds.
- Server port: `5202`.
- Throughput: `4.000 Mbit/s`.
- UDP loss: `37.936%`.
- UDP jitter: `3.453 ms`.
- Ping average/p95/loss: `47.321 ms` / `210.0 ms` / `28.0%`.
- Path verification: `PASS`.

Validated LTE1 exact workflow:

- Tag: `smoke_lte1_patched`.
- Profile: UDP `6M`, 1200-byte payload, 120 seconds.
- Server port: `5202`.
- Throughput: `6.000 Mbit/s`.
- UDP loss: `11.021%`.
- UDP jitter: `1.390 ms`.
- Ping average/p95/loss: `35.767 ms` / `84.8 ms` / `0.0%`.
- Path verification: `PASS`.

Rejected LTE2 setup smoke:

- Tag: `setup_smoke_lte2`.
- Profile: UDP `4M`, 1200-byte payload, 30 seconds.
- Result: `FAIL_OR_COUNTER_PARSE`.
- Reason: `lte2` counters stayed at zero while `lte1` carried the traffic;
  RouterOS reports `lte2` as `SIM not inserted`.

Failed and rejected runs remain in individual result folders for diagnosis, but
the cleaned `results/summary.csv` contains only validated rows.
