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

## Homebrew app upgrades

The `homebrew` role upgrades installed `iterm2`, `wezterm`, and `wezterm@nightly` casks first
with `--no-quit`, so Homebrew does not close the terminal running the playbook. Other outdated
packages are then upgraded in batches that exclude those casks, including nightly casks that
can still appear outdated immediately after an upgrade. This uses the existing package metadata
queries and adds upgrade commands only when a matching cask is outdated.

Override `confmgmt.homebrew.no_quit_casks` to replace that list; an empty list disables this
behaviour. The list affects upgrades of installed casks, including those absent from the
playbook's `casks` list, and does not request additional installations. Homebrew must support
`upgrade --no-quit`. The app bundle is still replaced on disk; restart the app when convenient.

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
