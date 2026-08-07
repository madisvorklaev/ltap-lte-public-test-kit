# LtAP LTE public iPerf test kit

Portable Linux Mint / Ubuntu-family test kit for repeatable dual-LTE measurements through a MikroTik LtAP.

The current lab method is source-IP based:

- `192.168.101.201` selects `lte1`
- `192.168.101.202` selects `lte2`
- public iPerf3 server/IP is pinned per campaign
- ping, iPerf3, RouterOS LTE telemetry, interface counters, and raw outputs are saved per run

Keep this repository private. Raw LTE telemetry may contain cellular identifiers and network details.

## Install on a new PC

```bash
git clone git@github.com:madisvorklaev/ltap-lte-public-test-kit.git
cd ltap-lte-public-test-kit
chmod +x install_linux_mint.sh scripts/*.sh scripts/ltap-test-routing
./install_linux_mint.sh
```

## Linux network setup

Identify the Ethernet interface connected to the LtAP:

```bash
ip -br link
ip -br -4 addr
nmcli -t -f NAME,UUID,TYPE,DEVICE connection show
```

Create or update a dedicated NetworkManager profile. Replace `eno1` if the LtAP is on another interface.

```bash
LTAP_IF=eno1

sudo arping -D -I "$LTAP_IF" 192.168.101.200
sudo arping -D -I "$LTAP_IF" 192.168.101.201
sudo arping -D -I "$LTAP_IF" 192.168.101.202

sudo nmcli con add type ethernet ifname "$LTAP_IF" con-name "LtAP-Lab" \
  ipv4.method manual \
  ipv4.addresses "192.168.101.200/24,192.168.101.201/24,192.168.101.202/24" \
  ipv4.never-default yes \
  ipv6.method disabled

sudo nmcli con up "LtAP-Lab"
```

Install persistent source-policy routing:

```bash
echo "201 ltap-lte1" | sudo tee /etc/iproute2/rt_tables.d/ltap-test.conf
echo "202 ltap-lte2" | sudo tee -a /etc/iproute2/rt_tables.d/ltap-test.conf

sudo install -m 0755 scripts/ltap-test-routing /usr/local/sbin/ltap-test-routing
sudo install -m 0755 scripts/90-ltap-test-routing.dispatcher \
  /etc/NetworkManager/dispatcher.d/90-ltap-test-routing

sudo LTAP_IF="$LTAP_IF" /usr/local/sbin/ltap-test-routing apply
```

Verify routing:

```bash
ip route get 1.1.1.1 from 192.168.101.201
ip route get 1.1.1.1 from 192.168.101.202
ping -c 3 -I 192.168.101.200 192.168.101.254
```

Both `route get` commands must use `via 192.168.101.254 dev <LtAP interface>` with the matching source IP.

## Router expectations

The collector does not add RouterOS routing, NAT, firewall, APN, band, SIM, or firmware settings.

The router should already contain the lab-enabled production behavior:

- UDP destination ports `5001-5020` route to `to-lte1`
- UDP destination ports `5021-5040` route to `to-lte2`
- source `192.168.101.201/32` routes to `to-lte1`
- source `192.168.101.202/32` routes to `to-lte2`
- the two source rules are before the production port rules and use `passthrough=no`
- per-LTE strict routes and masquerade rules exist

## Configure collector

Create a local config. Do not commit it.

```bash
cp config.example.json config.json
editor config.json
```

Required values are the local LtAP-facing interface, router address/user/key, source IPs, LTE interface names, and MikroTik source-rule comments.

The active lab values used on this machine are:

- router: `192.168.101.254`
- management IP: `192.168.101.200`
- LTE1 source: `192.168.101.201`
- LTE2 source: `192.168.101.202`

## Campaign

This repository includes the current pinned campaign files:

- `campaign.json`: sequential tests, server `iperf-ams-nl.eranium.net`, pinned IPv4 `217.18.95.142`
- `campaign-dual-lte1.json`: dual LTE1 port pool
- `campaign-dual-lte2.json`: dual LTE2 port pool

To start a new campaign, pin a new public iPerf3 host explicitly:

```bash
python3 ltap_public_test.py --config config.json campaign-init \
  --server <hostname> \
  --ports 5201 5202 5203 5204 5205 5206 5207 5208 5209 5210 \
  --campaign campaign-YYYYMMDD.json
```

Do not silently change server hostname/IP inside one comparison campaign.

## Run tests

LTE1 upload:

```bash
python3 ltap_public_test.py --config config.json run \
  --campaign campaign.json \
  --path lte1 \
  --protocol udp \
  --bitrate 6M \
  --packet-length 1200 \
  --duration 120 \
  --tag smoke_lte1
```

LTE2 upload:

```bash
python3 ltap_public_test.py --config config.json run \
  --campaign campaign.json \
  --path lte2 \
  --protocol udp \
  --bitrate 6M \
  --packet-length 1200 \
  --duration 120 \
  --tag smoke_lte2
```

Dual 5-minute upload:

```bash
python3 ltap_public_test.py --config config.json run \
  --campaign campaign-dual-lte1.json \
  --path lte1 --protocol udp --bitrate 6M --packet-length 1200 \
  --duration 300 --tag dual_lte1 &

python3 ltap_public_test.py --config config.json run \
  --campaign campaign-dual-lte2.json \
  --path lte2 --protocol udp --bitrate 6M --packet-length 1200 \
  --duration 300 --tag dual_lte2 &

wait
```

## Results

Each run writes a timestamped folder under `results/` containing:

- `test.json`
- `router_metadata.json`
- `telemetry.jsonl`
- `events.jsonl`
- `ping.txt`
- `iperf.json`
- `summary.json`
- stderr/error files

Validated single-path runs append to `results/summary.csv`. Dual runs may report `WARN_BACKGROUND_OTHER_LTE` because the other modem is intentionally active; inspect the per-run `summary.json` for both source-rule and LTE counter deltas.

After new tests on this machine, push results with:

```bash
./scripts/push_results.sh
```

## Current Results

See `RESULTS_INDEX.md` for a compact index of the result folders committed in this repository.

