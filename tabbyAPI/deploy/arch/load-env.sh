# Parse tabby.env without executing it.
# Values may be unquoted or wrapped in matching quotes. Shell metacharacters
# in unquoted or double-quoted values are refused.
load_tabby_env_file() {
  local env_file="$1" line key value
  [[ -f "$env_file" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" =~ ^[[:space:]]*$ ]] && continue
    if [[ ! "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
      continue
    fi
    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    case "$key" in
      LD_* | PYTHON* | BASH_* | SHELLOPTS | ENV | GLOBIGNORE | CDPATH | IFS)
        continue
        ;;
    esac
    if [[ "$value" =~ ^\'.*\'$ ]]; then
      value="${value:1:${#value}-2}"
      if [[ "$value" == *'$'* || "$value" == *'`'* ]]; then
        echo "Refusing $env_file: $key has an unsafe quoted value" >&2
        return 1
      fi
    elif [[ "$value" =~ ^\".*\"$ ]]; then
      value="${value:1:${#value}-2}"
      if [[ "$value" == *'$'* || "$value" == *'`'* || "$value" == *'\'* ]]; then
        echo "Refusing $env_file: $key has an unsafe quoted value" >&2
        return 1
      fi
    elif [[ ! "$value" =~ ^[A-Za-z0-9._/~:@%=+,+-]*$ ]]; then
      echo "Refusing $env_file: $key has an unsafe value" >&2
      return 1
    fi
    printf -v "$key" '%s' "$value"
    export "$key"
  done < "$env_file"
}
