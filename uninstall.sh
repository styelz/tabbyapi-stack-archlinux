#!/usr/bin/env bash
# Remove a tabbyapi-stack install.
#
# Services and processes come down first, files second. Deleting the tree while
# the user unit is still enabled leaves a running process holding the API port
# and the GPU with no files behind it, and linger brings it back at boot.
#
# Never touches: pacman packages, the NVIDIA driver, pyenv, ~/.ssh keys, or the
# USB / other source tree you copied weights from.
set -euo pipefail

SELF_ROOT="$(cd "$(dirname "$0")" && pwd)"

DEST=""
MODE="keep"
DRY_RUN=0
ASSUME_YES=0
DISABLE_LINGER=0

UNITS=(tabbyapi comfyui tabby-install-resume)
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
RESUME_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/tabbyapi-stack"
OLD_RESUME_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/tabby-stack"
AUTOSTART="$HOME/.config/autostart/tabbyapi-stack-install-resume.desktop"
OLD_AUTOSTART="$HOME/.config/autostart/tabby-stack-install-resume.desktop"

usage() {
  cat <<EOF
Usage: $(basename "$0") [options]

Stops the tabbyapi-stack user services, kills anything still running from the
install, and removes the install tree.

Options
  --dest PATH        Install root to remove. Default: found from the systemd
                     unit, then tabby.env, then \$HOME/tabbyapi-stack.
  --keep-models      Keep model weights and generated images (default).
  --purge            Remove the whole install root, weights included.
  --dry-run          Print what would happen; change nothing.
  --yes              Do not ask for confirmation.
  --disable-linger   Also run: loginctl disable-linger \$USER
  -h, --help         This text.

Kept by default (they are slow or impossible to get back):
  <root>/tabbyAPI/models          model weights
  <root>/tabbyAPI/pasted-images   images you generated
  <root>/ComfyUI/models           checkpoints, unet, vae, loras

Not removed in any mode: pacman packages, the NVIDIA driver, pyenv and its
shell rc lines, ~/.ssh keys, and the source tree you installed from.
EOF
}

while (($#)); do
  case "$1" in
    --dest) DEST="${2:-}"; shift 2 ;;
    --dest=*) DEST="${1#*=}"; shift ;;
    --keep-models) MODE="keep"; shift ;;
    --purge) MODE="purge"; shift ;;
    --dry-run|-n) DRY_RUN=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    --disable-linger) DISABLE_LINGER=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -n "$DEST" && "$DEST" != /* ]]; then
  DEST="$PWD/$DEST"
fi
# Work from / so that neither this shell nor its subshells have a working
# directory under the tree we are about to delete and scan for processes.
cd /

need_cmd() {
  command -v "$1" >/dev/null 2>&1
}

die() {
  echo "$*" >&2
  exit 1
}

run() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '  would run     %s\n' "$*"
  else
    "$@" || true
  fi
}

remove_path() {
  local p="$1"
  [[ -e "$p" || -L "$p" ]] || return 0
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '  would remove  %s\n' "$p"
  else
    rm -rf -- "$p"
    printf '  removed       %s\n' "$p"
  fi
}

# Deliberately strict: a lone AGENTS.md or start.sh is not enough to make a
# directory a deletion target.
looks_like_install() {
  local d="$1"
  [[ -d "$d/tabbyAPI" ]] && return 0
  [[ -f "$d/start.sh" && -f "$d/AGENTS.md" ]] && return 0
  return 1
}

dest_is_sane() {
  case "$1" in
    "" | "/" | "$HOME" | /usr | /usr/* | /etc | /etc/* | /var | /var/* | /boot | /boot/*)
      return 1
      ;;
  esac
  [[ "$1" == /* ]]
}

env_install_root() {
  local env_file="$1/tabbyAPI/deploy/arch/tabby.env"
  [[ -f "$env_file" ]] || return 1
  local line
  line="$(grep -m1 '^TABBY_INSTALL_ROOT=' "$env_file" 2>/dev/null || true)"
  [[ -n "$line" ]] || return 1
  printf '%s' "${line#TABBY_INSTALL_ROOT=}"
}

# The unit keeps working even when the tree it points at has been deleted, so
# it is the most reliable source for an install someone already rm -rf'd.
discover_dest() {
  local wd root
  if [[ -n "$DEST" ]]; then
    printf '%s' "$DEST"
    return 0
  fi
  # Running from inside the install itself, but never from the source checkout.
  if looks_like_install "$SELF_ROOT" && [[ ! -d "$SELF_ROOT/.git" ]]; then
    printf '%s' "$SELF_ROOT"
    return 0
  fi
  if need_cmd systemctl; then
    wd="$(systemctl --user show tabbyapi -p WorkingDirectory --value 2>/dev/null || true)"
    if [[ -n "$wd" && "$wd" != "n/a" ]]; then
      printf '%s' "${wd%/tabbyAPI}"
      return 0
    fi
  fi
  if root="$(env_install_root "$HOME/tabbyapi-stack")"; then
    printf '%s' "$root"
    return 0
  fi
  if root="$(env_install_root "$HOME/tabby-stack")"; then
    printf '%s' "$root"
    return 0
  fi
  if [[ -d "$HOME/tabbyapi-stack" ]]; then
    printf '%s' "$HOME/tabbyapi-stack"
    return 0
  fi
  if [[ -d "$HOME/tabby-stack" ]]; then
    printf '%s' "$HOME/tabby-stack"
    return 0
  fi
  return 1
}

# PIDs whose binary or working directory lives under the install. Matches
# processes whose files were already deleted; /proc reports those with a
# " (deleted)" suffix.
procs_under_dest() {
  local p pid exe cwd
  for p in /proc/[0-9]*; do
    pid="${p#/proc/}"
    [[ "$pid" == "$$" || "$pid" == "$PPID" ]] && continue
    exe="$(readlink "$p/exe" 2>/dev/null || true)"
    cwd="$(readlink "$p/cwd" 2>/dev/null || true)"
    exe="${exe% (deleted)}"
    cwd="${cwd% (deleted)}"
    if [[ "$exe" == "$DEST"/* || "$cwd" == "$DEST"/* ]]; then
      printf '%s\n' "$pid"
    fi
  done
}

port_pids() {
  local port="$1"
  need_cmd ss || return 0
  ss -ltnp 2>/dev/null \
    | awk -v p=":$port" '$4 ~ p"$"' \
    | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u
}

describe_pid() {
  local pid="$1" cmd
  cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
  printf '%s' "${cmd:-(gone)}"
}

DEST="$(discover_dest || true)"
[[ -n "$DEST" ]] || die "No tabbyapi-stack install found. Pass --dest /path/to/tabbyapi-stack."
DEST="${DEST%/}"

dest_is_sane "$DEST" || die "Refusing to act on $DEST. Pass an explicit --dest inside your home or data disk."
if [[ -d "$DEST/.git" ]]; then
  die "Refusing: $DEST is a git checkout, so it is the source tree, not an install."
fi
if [[ -e "$DEST" ]] && ! looks_like_install "$DEST"; then
  die "Refusing: $DEST exists but has no tabbyAPI/ (and no start.sh + AGENTS.md pair)."
fi

TABBY_PORT=5000
COMFY_PORT=8188
if [[ -f "$DEST/tabbyAPI/deploy/arch/tabby.env" ]]; then
  line="$(grep -m1 '^TABBY_NETWORK_PORT=' "$DEST/tabbyAPI/deploy/arch/tabby.env" 2>/dev/null || true)"
  [[ -n "$line" ]] && TABBY_PORT="${line#TABBY_NETWORK_PORT=}"
  line="$(grep -m1 '^COMFYUI_URL=' "$DEST/tabbyAPI/deploy/arch/tabby.env" 2>/dev/null || true)"
  if [[ -n "$line" ]]; then
    line="${line#COMFYUI_URL=}"
    line="${line#*://}"
    line="${line%%/*}"
    [[ "$line" == *:* ]] && COMFY_PORT="${line##*:}"
  fi
fi

KEEP=()
if [[ "$MODE" == "keep" ]]; then
  KEEP=(
    "$DEST/tabbyAPI/models"
    "$DEST/tabbyAPI/pasted-images"
    "$DEST/ComfyUI/models"
  )
fi

is_keep_exact() {
  local p="$1" k
  for k in ${KEEP+"${KEEP[@]}"}; do
    [[ "$p" == "$k" ]] && return 0
  done
  return 1
}

keep_below() {
  local p="$1" k
  for k in ${KEEP+"${KEEP[@]}"}; do
    [[ "$k" == "$p"/* ]] && return 0
  done
  return 1
}

prune_dir() {
  local dir="$1" entry
  while IFS= read -r -d '' entry; do
    if is_keep_exact "$entry"; then
      printf '  kept          %s\n' "$entry"
    elif keep_below "$entry"; then
      prune_dir "$entry"
    else
      remove_path "$entry"
    fi
  done < <(find "$dir" -mindepth 1 -maxdepth 1 -print0 2>/dev/null)
}

echo "tabbyapi-stack uninstall"
echo "  install root:  $DEST"
if [[ -e "$DEST" ]]; then
  echo "  tree on disk:  yes ($(du -sh "$DEST" 2>/dev/null | cut -f1 || echo '?'))"
else
  echo "  tree on disk:  already deleted"
fi
if [[ "$MODE" == "purge" ]]; then
  echo "  weights:       REMOVED (--purge)"
else
  echo "  weights:       kept (models/, pasted-images/)"
fi
[[ "$DRY_RUN" -eq 1 ]] && echo "  mode:          dry run, nothing will change"

echo
echo "Services"
found_unit=0
for u in "${UNITS[@]}"; do
  active=""
  enabled=""
  if need_cmd systemctl; then
    active="$(systemctl --user is-active "$u" 2>/dev/null || true)"
    enabled="$(systemctl --user is-enabled "$u" 2>/dev/null || true)"
  fi
  if [[ -f "$UNIT_DIR/$u.service" || "$active" == "active" || "$enabled" == "enabled" ]]; then
    printf '  %-22s %s\n' "$u.service" "${active:-unknown}/${enabled:-unknown}"
    found_unit=1
  fi
done
[[ "$found_unit" -eq 0 ]] && echo "  (none)"

echo
echo "Processes running from $DEST"
mapfile -t DEST_PIDS < <(procs_under_dest)
if ((${#DEST_PIDS[@]})); then
  for pid in "${DEST_PIDS[@]}"; do
    printf '  %-8s %s\n' "$pid" "$(describe_pid "$pid")"
  done
else
  echo "  (none)"
fi

echo
echo "Removing Code sandbox containers"
if need_cmd docker; then
  mapfile -t BOX_IDS < <(docker ps -aq --filter label=tabby.stack=code 2>/dev/null || true)
  if ((${#BOX_IDS[@]})); then
    run docker rm -f "${BOX_IDS[@]}"
  else
    echo "  (none)"
  fi
else
  echo "  docker not installed; skipping"
fi

is_dest_pid() {
  local want="$1" pid
  for pid in ${DEST_PIDS+"${DEST_PIDS[@]}"}; do
    [[ "$pid" == "$want" ]] && return 0
  done
  return 1
}

FOREIGN=()
for port in "$TABBY_PORT" "$COMFY_PORT"; do
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    is_dest_pid "$pid" && continue
    FOREIGN+=("$port:$pid")
  done < <(port_pids "$port")
done
if ((${#FOREIGN[@]})); then
  echo
  echo "Other processes on the tabbyapi-stack ports (left alone):"
  for entry in "${FOREIGN[@]}"; do
    printf '  port %-6s pid %-8s %s\n' "${entry%%:*}" "${entry##*:}" "$(describe_pid "${entry##*:}")"
  done
fi

if [[ "$DRY_RUN" -eq 0 && "$ASSUME_YES" -eq 0 ]]; then
  echo
  read -r -p "Type 'remove' to continue: " answer || true
  [[ "$answer" == "remove" ]] || die "Cancelled."
fi

echo
echo "Stopping services"
if need_cmd systemctl; then
  for u in "${UNITS[@]}"; do
    if ! systemctl --user cat "$u.service" >/dev/null 2>&1; then
      continue
    fi
    run systemctl --user disable --now "$u.service"
  done
else
  echo "  systemctl not available; skipping"
fi

echo
echo "Stopping TTY screensaver, GPU fan unit, and removing tsctl"
if need_cmd systemctl; then
  run sudo -n systemctl disable --now tabby-saver
  run sudo -n rm -f /etc/systemd/system/tabby-saver.service
  run sudo -n systemctl disable --now tabby-gpu
  run sudo -n rm -f /etc/systemd/system/tabby-gpu.service
  run sudo -n systemctl daemon-reload
fi
run sudo -n rm -f /usr/local/bin/tsctl \
  /usr/share/bash-completion/completions/tsctl \
  /usr/share/zsh/site-functions/_tsctl

echo
echo "Stopping leftover processes"
mapfile -t DEST_PIDS < <(procs_under_dest)
if ((${#DEST_PIDS[@]})); then
  if [[ "$DRY_RUN" -eq 1 ]]; then
    for pid in "${DEST_PIDS[@]}"; do
      printf '  would kill    %s  %s\n' "$pid" "$(describe_pid "$pid")"
    done
  else
    kill -TERM "${DEST_PIDS[@]}" 2>/dev/null || true
    for _ in $(seq 1 15); do
      mapfile -t DEST_PIDS < <(procs_under_dest)
      ((${#DEST_PIDS[@]})) || break
      sleep 1
    done
    if ((${#DEST_PIDS[@]})); then
      echo "  still up after 15s, sending KILL: ${DEST_PIDS[*]}"
      kill -KILL "${DEST_PIDS[@]}" 2>/dev/null || true
      sleep 1
    fi
    mapfile -t DEST_PIDS < <(procs_under_dest)
    if ((${#DEST_PIDS[@]})); then
      echo "  WARNING: could not stop: ${DEST_PIDS[*]}"
    else
      echo "  all stopped"
    fi
  fi
else
  echo "  (none)"
fi

echo
echo "Removing unit files and install hooks"
for u in "${UNITS[@]}"; do
  remove_path "$UNIT_DIR/$u.service"
  remove_path "$UNIT_DIR/default.target.wants/$u.service"
done
remove_path "$RESUME_DIR"
remove_path "$OLD_RESUME_DIR"
remove_path "$AUTOSTART"
remove_path "$OLD_AUTOSTART"
if need_cmd systemctl; then
  run systemctl --user daemon-reload
  run systemctl --user reset-failed
fi

echo
echo "Removing files"
if [[ ! -e "$DEST" ]]; then
  echo "  nothing on disk"
elif [[ "$MODE" == "purge" ]]; then
  remove_path "$DEST"
else
  prune_dir "$DEST"
fi

if [[ "$DISABLE_LINGER" -eq 1 ]]; then
  echo
  echo "Disabling linger"
  if need_cmd loginctl; then
    run sudo -n loginctl disable-linger "$USER"
  else
    echo "  loginctl not available"
  fi
fi

echo
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "Dry run finished. Re-run without --dry-run to apply."
  exit 0
fi
echo "Uninstall finished."
kept_any=0
for k in ${KEEP+"${KEEP[@]}"}; do
  if [[ -e "$k" ]]; then
    printf '  kept  %s  (%s)\n' "$k" "$(du -sh "$k" 2>/dev/null | cut -f1 || echo '?')"
    kept_any=1
  fi
done
if [[ "$kept_any" -eq 1 ]]; then
  echo "  Re-installing to the same root reuses those weights."
fi
if need_cmd loginctl && [[ "$DISABLE_LINGER" -eq 0 ]]; then
  if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || true)" == "yes" ]]; then
    echo "  Linger is still on. Other user services may need it:"
    echo "    sudo loginctl disable-linger $USER"
  fi
fi
echo "  Packages, the NVIDIA driver, pyenv and ~/.ssh were left alone."
