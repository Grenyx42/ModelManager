# Deployment

Running the coordinator continuously, securing it, reaching it from
another machine, and controlling Windows boxes. See the main
[README](../README.md) for installation and the CLI quickstart.

## Run the coordinator

`./scripts/setup.sh` (see the README's [Setup](../README.md#setup)) already
installs `systemd/modelmanager.service` as a user unit and starts it for
you - this section is for running it some other way, or customizing that
unit.

```bash
modelmanager serve --port 8778   # binds 0.0.0.0 by default - reachable from systemB, systemC, etc.
```

`--host`/`--port` default to the `MODELMANAGER_HOST`/`MODELMANAGER_PORT` env
vars when set, else `0.0.0.0`/`8778` - a flag always wins over either. Serves
HTTPS (self-signed cert from `./scripts/setup.sh` - see
[Authentication & TLS](#authentication--tls)) and refuses to start at all if
that cert doesn't exist yet. Binding `0.0.0.0` means the REST API is
*reachable* by anything that can reach this box - on this setup that's the
tailnet, gated by Tailscale itself rather than anything in this app - but
every request still needs the bearer token below, so reachable isn't the
same as usable. Pass `--host 127.0.0.1` (or set `MODELMANAGER_HOST=127.0.0.1`)
instead if you want it local-only as well.

`systemA`'s system entry in `registry.json` needs `"advertise_host": "systemA"`
(its Tailscale name) for this to actually work end-to-end: without it,
`ensure`/`acquire` on a model running locally would hand a remote caller
back `{"host": "localhost", ...}`, which means *them*, not systemA.

If you ever need to install it by hand instead (the unit so pinned models,
e.g. an embedding model, come up automatically):

```bash
mkdir -p ~/.config/systemd/user
cp systemd/modelmanager.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now modelmanager.service
```

That unit already points at the fixed venv/registry locations `scripts/setup.sh`
and `default_registry_path()` use, so it works as-is regardless of where this
repo was cloned. It also reads `~/.config/modelmanager/env` as an
`EnvironmentFile` (missing is fine, nothing requires it) - put
`MODELMANAGER_HOST=...`/`MODELMANAGER_PORT=...`/`MODELMANAGER_REGISTRY=...`/etc.
in there (`scripts/setup.sh` offers to write the first two interactively) to
override any of `serve`'s defaults without touching the unit file itself,
then `systemctl --user daemon-reload && systemctl --user restart modelmanager`.

A `systemctl --user` unit only starts once *some* login session for that user
exists - fine for a desktop machine you're normally logged into, but if this
box might reboot unattended (no graphical login happening automatically),
also run `loginctl enable-linger $(whoami)` once so the user service manager
itself starts at boot regardless, and the coordinator (and any pinned models)
comes back without you logging in first.

## Authentication & TLS

Every REST request needs `Authorization: Bearer <token>` over HTTPS - no
exception for a "just reading" endpoint, and no way to run the coordinator
without both configured. `./scripts/setup.sh` generates both automatically
on first run - the token at `~/.config/modelmanager/token` (0600), a
self-signed TLS cert/key at `~/.config/modelmanager/tls/` (key 0600) - same
XDG-style fixed location as `registry.json`. The CLI picks both up with no
configuration of its own, and since `Panel.qml` only ever shells out to the
CLI, the bar plugin authenticates (and verifies TLS) for free too - this
only matters to you if something *other* than the CLI/plugin needs to call
the REST API directly (your own app, a script):

```bash
modelmanager auth show      # print the current token (and where it came from)
modelmanager auth rotate    # generate and switch to a new one, no restart needed
```

A few things worth knowing:

- **`MODELMANAGER_TOKEN`** (env var) overrides the token file entirely, for
  both the coordinator and the CLI - useful for a systemd `EnvironmentFile`
  (`~/.config/modelmanager/env` - see [Run the coordinator](#run-the-coordinator))
  or a container secret instead of a file on disk. Likewise
  `MODELMANAGER_TLS_CERT_PATH`/`MODELMANAGER_TLS_KEY_PATH` for the cert/key,
  and `MODELMANAGER_HOST`/`MODELMANAGER_PORT` for what `serve` binds to.
- **Rotating the token takes effect immediately** - the coordinator reads it
  fresh on every request rather than caching it at startup, so there's
  nothing to restart after `modelmanager auth rotate`. Update whatever else
  holds the old value (another app, a saved `curl` command) at the same time,
  since it'll start getting 401s the instant you rotate.
- **The token doesn't expire** - it's valid until you manually rotate it.
  That's a deliberate simplicity tradeoff for a single-operator home-lab
  tool, not an oversight; treat it like an SSH private key, not a
  low-stakes API key, since holding it means being able to start/stop
  anything and rewrite the registry (see the architecture notes in the
  [README](../README.md) on what that implies for remote systems).
- **The cert is self-signed** (there's no public CA to ask for a box that
  only your own machines ever talk to) and covers `localhost`, `127.0.0.1`,
  and this machine's own hostname - not a Tailscale MagicDNS name, which
  `setup.sh` has no reliable way to know in advance. A caller on another box
  needs its own copy of `tls/cert.pem` to verify against (the CLI falls back
  to the system CA bundle if it doesn't have one, which will fail closed
  with a clear TLS error, not silently accept an unverified connection).
- **An existing install upgrading into this version** won't have a
  token/cert yet, and the coordinator will refuse to start (missing cert) or
  refuse every request with a clear `AUTH_NOT_CONFIGURED` error (missing
  token) until they exist - re-run `./scripts/setup.sh` (safe, idempotent),
  or `modelmanager auth rotate` for just the token.
- This closes the "anyone on the LAN can read or hit the coordinator's REST
  API" gap, but it's not a substitute for a trusted network - keep the
  coordinator off the open internet regardless.

## Sharing access with another person

The owner token (above) has full access to everything - rotate/revoke it and
every caller using it loses access, there's no narrower grain for it. For
sharing specific systems with someone else (a friend on your tailnet, another
box you don't fully trust with the whole fleet), `modelmanager grant`
associates a *different* token - one that person generated for their own
coordinator, not yours - with a name, and then separately grants or revokes
access to individual systems from your `registry.json` under that name, as
many times as you like, without ever retyping the token:

```bash
modelmanager grant add bob <the-token-bob-shared-with-you>   # no system access yet
modelmanager grant allow bob systemB
modelmanager grant allow bob systemC
modelmanager grant revoke bob systemB     # bob keeps systemC
modelmanager grant list                   # name/allowed_systems/created_at - never the token
modelmanager grant remove bob             # deletes the identity entirely
```

This is local-only (reads/writes `~/.config/modelmanager/grants.json`
directly, like `auth show`/`rotate`) - no coordinator restart needed, same as
rotating the owner token. Re-running `grant add` for a name that already
exists rotates its token but leaves `allowed_systems` untouched - handy when
bob rotates his own token without losing whatever you'd already granted him.
Once allowed a system, a request bearing that token can read and operate
(`ensure`/`stop`/`verify`/`logs`/group actions) on anything tied to it, but
can never write registry config, and never sees `doctor`,
`/v1/config/validate`, or the access/lifecycle logs - those stay owner-only
regardless of `allowed_systems`. Access is per-*system*, not per-model: if a
model has candidates split across more than one system, a grant covering any
one of them can reach the whole model (see `Manager.model_systems` for why) -
keep candidates for a shared model on one system if you need tighter
isolation than that. A `registry.json` **group** only shows up for a grant
at all (`GET /v1/groups`, a single group by name, or the `groups` section of
`GET /v1/config`) if *every* member model is within its allowed systems - a
group mixing permitted and non-permitted models is hidden outright rather
than shown with the non-permitted members quietly dropped, since a partial
view would itself leak that other models exist and share a group with ones
the grant can see. Group *actions* (`group-ensure`/`group-stop`/etc.) aren't
affected by this - those still operate best-effort per member, gated by each
member model's own system check, same as any other per-member failure.

`grant remove` is this project's one deliberate exception to "there is no
delete endpoint, edit the JSON by hand" (see CONTRIBUTING.md) - revocability
is a real security requirement for a shared credential in a way it isn't for
registry resources. `grant allow`/`revoke` aren't deletes in that sense -
they mutate an existing grant's `allowed_systems` list in place, same as any
other field update.

## Using `modelmanager` from another machine

The CLI defaults to `https://127.0.0.1:8778` - i.e. "the coordinator is on
this same box" - which is correct for the box actually running it, but not
for a second machine (another box of yours, or a friend's, on the same
tailnet) that wants to call a coordinator running elsewhere. Three things
make that work, all copied from the coordinator box's
`~/.config/modelmanager/`:

1. **Point it at the right host.** Set `MODELMANAGER_URL` to the
   coordinator's reachable address - its Tailscale name/IP, not `localhost`:
   ```bash
   export MODELMANAGER_URL=https://systemA:8778
   ```
   Unset (the default), every `modelmanager` command stays local-only - it
   never silently reaches across the network for you.
2. **Copy the token** (`~/.config/modelmanager/token` on the coordinator
   box) and set it on this one:
   ```bash
   export MODELMANAGER_TOKEN=<the token value - `modelmanager auth show` on the coordinator box prints it>
   ```
3. **Copy the cert** (`~/.config/modelmanager/tls/cert.pem` - not `key.pem`,
   that one never leaves the coordinator box) somewhere on this machine and
   point at it:
   ```bash
   export MODELMANAGER_TLS_CERT_PATH=/path/to/your/copy/of/cert.pem
   ```
   Skip this and the CLI falls back to the system CA bundle, which won't
   recognize a self-signed cert - every call fails closed with a clear TLS
   error rather than silently trusting whatever's on the other end.

All three are plain env vars, so a shell profile or `.env` on that second
machine is enough to make every bare `modelmanager ...` command there behave
exactly like it does locally on the coordinator box. None of this applies to
the Omarchy bar plugin - it only ever talks to a coordinator running on its
own box (see the architecture notes in the README), so it has nothing to
configure here.

### Talking to someone else's coordinator (`hosts`/`--peer`)

The three env vars above assume you own both machines and can just copy
`cert.pem` over yourself. For a coordinator *you don't own* - e.g. after
someone else ran `modelmanager grant add` for you (see "Sharing access with
another person" above) - you don't have filesystem access to their
`tls/cert.pem`, so there's nothing to copy. `modelmanager hosts` fetches it
over the wire instead and has you confirm its fingerprint once
(trust-on-first-connect, the same model SSH uses for host keys):

```bash
modelmanager hosts probe https://systemA:8778      # fingerprint only, nothing saved
modelmanager hosts add alice https://systemA:8778  # prints the fingerprint, asks you to confirm
modelmanager hosts list
modelmanager hosts remove alice
```

Confirm the printed fingerprint against what alice tells you out-of-band
(chat, in person - not through the same channel that gave you the URL)
before accepting; `--yes` skips the prompt for scripting, but only use it
when you've already verified the fingerprint some other way. Once trusted,
target that coordinator by name instead of juggling `MODELMANAGER_URL`/
`_TOKEN`/`_TLS_CERT_PATH` yourself:

```bash
modelmanager --peer alice list
modelmanager --peer alice ensure some-model
```

Your own token (the same one you'd use locally) is what gets sent - whatever
systems alice granted it covers, nothing more (see `modelmanager grant`
above). `--peer` and the three env vars are independent mechanisms; use
whichever fits - `--peer` for a coordinator you've TOFU-trusted by name,
the env vars for one you fully own and can copy files from directly.

## Windows boxes

Nothing to copy by hand: the coordinator deploys `scripts/windows/agent.ps1`
itself to `~\.ModelManager\agent.ps1` (the SSH user's own home directory - the
exact `C:\Users\<name>` path is resolved over SSH, never assumed) on every
Windows system with `"control": "ssh"`, automatically and best-effort on
every startup, skipping the upload if the file's already there unchanged.
To push a change without restarting, or target one box:

```bash
modelmanager deploy-agent            # every windows/ssh system
modelmanager deploy-agent systemB    # just one
modelmanager deploy-agent systemB --force    # re-upload even if unchanged
```

Just make sure `ssh <that-host>` already works non-interactively (key auth,
no passphrase prompt) - `~/.ssh/config` aliases are reused as-is.

**Why `agent.ps1` launches via WMI instead of `Start-Process`:** Windows'
OpenSSH assigns every SSH session's processes to a Job Object, and kills the
whole job the instant the session closes - which is right after `agent.ps1`
returns. A plain `Start-Process` child stays in that job and gets killed
along with it, so the model would appear to start successfully and then die
within moments, often with zero log output if it happened mid-load. WMI
process creation (`Invoke-CimMethod ... Win32_Process Create`) makes
`WmiPrvSE.exe` the parent instead, entirely outside that job, so the process
survives past the SSH session ending - the actual point of starting a
background service. Confirmed directly: a `Start-Process`-launched PID was
gone the moment a fresh SSH connection checked it; a WMI-launched one wasn't.
