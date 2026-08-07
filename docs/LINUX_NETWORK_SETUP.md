# Linux Network Setup

This workstation uses a dedicated NetworkManager profile for the LtAP lab link.

- Linux interface: `eno1`
- NetworkManager profile: `LtAP-Lab`
- Management address: `192.168.101.200/24`
- LTE1 test source: `192.168.101.201/24`
- LTE2 test source: `192.168.101.202/24`
- LtAP gateway: `192.168.101.254`

The profile has `ipv4.never-default yes` so the wired lab link does not become
the normal workstation default route.

Source-policy routing is handled by:

- `/etc/iproute2/rt_tables.d/ltap-test.conf`
- `/usr/local/sbin/ltap-test-routing`
- `/etc/NetworkManager/dispatcher.d/90-ltap-test-routing`

Policy tables:

```text
201 ltap-lte1
202 ltap-lte2
```

Rules:

```text
priority 20100 from 192.168.101.201/32 lookup ltap-lte1
priority 20200 from 192.168.101.202/32 lookup ltap-lte2
```

Expected route checks:

```bash
ip route get 1.1.1.1 from 192.168.101.201
ip route get 1.1.1.1 from 192.168.101.202
```

Both should use `eno1`, gateway `192.168.101.254`, and the matching source
address.
