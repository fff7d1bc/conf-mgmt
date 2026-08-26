# conf-mgmt

Configuration as a code for operating systems, built around Ansible.

## Supported operating systems

- Fedora Kinoite (Fedora Atomic)
- Ubuntu
- Raspberry Pi OS (Debian derivative)
- MacOS

## Usage

```
./runner local
./runner local NAME.yml
./runner syntaxcheck NAME.yml
./runner remote NAME.yml
./runner remote NAME.yml host
./runner remote NAME.yml user@host
./runner local NAME.yml -- --tags home_files
./runner local NAME.yml -- --skip-tags homebrew
```

- When using `local` and not passing the name of playbook, hostname.yml will be used.
- `syntaxcheck` runs the given playbook through the local execution path with `--syntax-check`.
- For `remote`, if host is not provided, name of playbook without the `.yml` prefix is used as hostname. When user is not provided in target, it defaults to `root`.
- Everything after `--` is passed to `ansible-playbook`.
- Runs that report changes end with a controller-side summary of the changed task names.

## locals.yml

Special file `locals.yml` in the root of the repository is meant to hold additonal configuration that is not meant to be checked into git.

## macOS preferences

The `macos` role requires the account whose preferences it manages. Other host inputs are
recursively merged with the shared defaults, so a playbook can override only the values that differ:

```yaml
confmgmt:
  macos:
    user: piotr
    dock:
      orientation: bottom
```

`confmgmt.macos.user` is mandatory and independent of other roles. The role is skipped on
non-macOS hosts. Quit System Settings before applying the role so it cannot overwrite externally
managed preferences. The role does not restart applications or services. After a changed run, log
out and back in before expecting the managed preferences to take effect.
