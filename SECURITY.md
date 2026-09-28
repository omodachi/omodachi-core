# Security policy

## Reporting a vulnerability

Please report it privately, in either of two ways:

- **GitHub.** On this repository's **Security** tab, press **Report a
  vulnerability**
  (<https://github.com/omodachi/omodachi-core/security/advisories/new>). That
  opens a private security advisory which only you and the maintainer can see.
- **Email** <security@omodachi.app>.

Please do not put the details in a public issue, pull request or discussion.

Reports are read by the maintainer, Leo Zhang. There is no bug bounty.

## What to include

- The commit or release you looked at. On a computer the plugin installed,
  `git -C ~/.local/share/omodachi/src rev-parse HEAD` prints the commit.
- What an attacker needs (the same network, a paired device, a local account
  on the computer, ...) and what they get.
- Steps to reproduce it or a proof of concept, and what you expected instead.

## Scope

This repository: the host daemon, its installer `scripts/install_host.py`,
and what they write on the computer. The README's
[Security model](README.md#security-model) says who can do what and which code
decides it.

The other parts of Omodachi take reports the same two ways. Use the repository
the problem is in; if you are not sure which, email.

| Repository | What it is |
| --- | --- |
| [`omodachi-plugin`](https://github.com/omodachi/omodachi-plugin/security) | the Omarchy plugin: the bar icon, the panel and the Install button's bootstrap |
| [`omodachi-ios`](https://github.com/omodachi/omodachi-ios/security) | the iPhone and iPad app |
| [`omodachi-sunshine`](https://github.com/omodachi/omodachi-sunshine/security) | the Sunshine fork that streams the remote screen |
