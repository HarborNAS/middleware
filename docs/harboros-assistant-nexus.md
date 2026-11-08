# Nexus Assistant same-origin integration

The Halfmoon 26.04.0 nginx template serves WebUI from
`/usr/share/truenas/webui`. Both Assistant API prefixes pass through Gate on
loopback port 8787. The legacy Beacon URL is mapped to `/api/beacon`; browsers
cannot proxy directly to Beacon port 4174.

WebUI supplies a fresh, one-use `X-HarborOS-Auth-Token`. The Nexus Gate profile
verifies it against native middleware `auth.login_ex`, checks the current
NAS administrator role, resolves the configured workspace and then supplies
its per-device service credential and verified principal to Beacon. Incoming
Bearer tokens and client-supplied trusted principal headers are discarded.
No shared browser token or Demo edge process is required.

Install this middleware revision together with the Nexus Gate profile and
matching WebUI interceptor. Old standalone Gate setup HTML cannot obtain a
native NAS user token; use the native Connections page. Gate and Beacon
service credentials are generated on first boot, not during ISO assembly.

Validation includes actual Mako rendering for HTTP/TLS/FIPS, native nginx URI
and header forwarding, and native nginx against the actual Gate executable.
The latter mocks only middleware authentication and Beacon HTTP responses,
and requires an explicitly isolated Docker fixture. These tests do not certify
NAS installation, boot environments, real systemd startup or hardware.
