# Security notes

- `config.env`, `.env`, `data/`, CA private keys, cookies, and runtime logs are deployment secrets and are ignored.
- Bind the WebUI and MITM listener to the trusted LAN address or protect them with a host firewall.
- The mitmproxy CA private key can decrypt HTTPS traffic from any device that trusts it. Treat it as a high-sensitivity secret.
- The router hook matches only `gs-loc.apple.com` and `gs-loc-cn.apple.com` and excludes the router and Docker server to prevent loops.
- Do not expose the management WebUI or the CA private key to the public Internet.
