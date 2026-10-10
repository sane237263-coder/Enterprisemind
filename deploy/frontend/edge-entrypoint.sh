#!/bin/sh
set -eu

TEMPLATE=/usr/share/nginx/nginx.conf.template
CONFIG=/tmp/nginx.conf

fail_config() {
    printf '%s\n' 'edge public origin configuration is invalid' >&2
    exit 64
}

if [ ! -r "$TEMPLATE" ] || ! command -v awk >/dev/null 2>&1 || \
    ! command -v sed >/dev/null 2>&1 || ! command -v grep >/dev/null 2>&1; then
    fail_config
fi

render_values=$(LC_ALL=C awk '
function valid_port(value) {
    return value ~ /^[1-9][0-9]*$/ && length(value) <= 5 && value + 0 <= 65535
}

function valid_ipv4(value, octets, count, i, octet, normalized) {
    if (length(value) > 15) return 0
    count = split(value, octets, ".")
    if (count != 4) return 0
    normalized = ""
    for (i = 1; i <= 4; i++) {
        octet = octets[i]
        if (octet !~ /^[0-9][0-9]*$/ || length(octet) > 3) return 0
        if (length(octet) > 1 && substr(octet, 1, 1) == "0") return 0
        if (octet + 0 > 255) return 0
        if (i > 1) normalized = normalized "."
        normalized = normalized octet
    }
    IPV4_NORMALIZED = normalized
    return 1
}

function valid_dns(value, labels, count, i, label, first, last) {
    if (length(value) < 1 || length(value) > 253) return 0
    if (substr(value, 1, 1) == "." || substr(value, length(value), 1) == ".") return 0
    count = split(value, labels, ".")
    for (i = 1; i <= count; i++) {
        label = labels[i]
        if (length(label) < 1 || length(label) > 63 || label ~ /[^A-Za-z0-9-]/) return 0
        first = substr(label, 1, 1)
        last = substr(label, length(label), 1)
        if (first !~ /^[A-Za-z0-9]$/ || last !~ /^[A-Za-z0-9]$/) return 0
    }
    DNS_NORMALIZED = tolower(value)
    return 1
}

function valid_hex_word(value) {
    return length(value) >= 1 && length(value) <= 4 && value !~ /[^0-9A-Fa-f]/
}

function valid_ipv6(value, address, last_colon, ipv4_tail, ipv4_octets, double_pos,
                    left, right, left_parts, right_parts, all_parts, left_count,
                    right_count, count, i, j, words, word, zero_start, zero_count,
                    best_start, best_count, before, after) {
    address = value
    if (address == "" || address ~ /[^0-9A-Fa-f:.]/) return 0

    if (index(address, ".") > 0) {
        last_colon = 0
        for (i = 1; i <= length(address); i++) {
            if (substr(address, i, 1) == ":") last_colon = i
        }
        if (last_colon == 0) return 0
        ipv4_tail = substr(address, last_colon + 1)
        if (!valid_ipv4(ipv4_tail)) return 0
        split(IPV4_NORMALIZED, ipv4_octets, ".")
        address = substr(address, 1, last_colon) \
            sprintf("%x:%x", ipv4_octets[1] * 256 + ipv4_octets[2], \
                    ipv4_octets[3] * 256 + ipv4_octets[4])
    }

    double_pos = index(address, "::")
    if (double_pos > 0 && index(substr(address, double_pos + 2), "::") > 0) return 0
    if (double_pos > 0) {
        left = substr(address, 1, double_pos - 1)
        right = substr(address, double_pos + 2)
        if (left ~ /(^:|:$)/ || right ~ /(^:|:$)/) return 0
        left_count = left == "" ? 0 : split(left, left_parts, ":")
        right_count = right == "" ? 0 : split(right, right_parts, ":")
        if (left_count + right_count >= 8) return 0
        for (i = 1; i <= left_count; i++) {
            if (!valid_hex_word(left_parts[i])) return 0
            words[i] = tolower(left_parts[i])
        }
        zero_count = 8 - left_count - right_count
        for (i = 1; i <= zero_count; i++) words[left_count + i] = "0"
        for (i = 1; i <= right_count; i++) {
            if (!valid_hex_word(right_parts[i])) return 0
            words[left_count + zero_count + i] = tolower(right_parts[i])
        }
    } else {
        if (substr(address, 1, 1) == ":" || substr(address, length(address), 1) == ":") return 0
        count = split(address, all_parts, ":")
        if (count != 8) return 0
        for (i = 1; i <= 8; i++) {
            if (!valid_hex_word(all_parts[i])) return 0
            words[i] = tolower(all_parts[i])
        }
    }

    for (i = 1; i <= 8; i++) {
        word = words[i]
        while (length(word) > 1 && substr(word, 1, 1) == "0") word = substr(word, 2)
        words[i] = word
    }

    best_start = 0
    best_count = 1
    zero_start = 0
    zero_count = 0
    for (i = 1; i <= 9; i++) {
        if (i <= 8 && words[i] == "0") {
            if (zero_count == 0) zero_start = i
            zero_count++
        } else {
            if (zero_count > best_count) {
                best_start = zero_start
                best_count = zero_count
            }
            zero_count = 0
        }
    }

    if (best_start == 0) {
        IPV6_NORMALIZED = words[1]
        for (i = 2; i <= 8; i++) IPV6_NORMALIZED = IPV6_NORMALIZED ":" words[i]
        return 1
    }
    before = ""
    after = ""
    for (i = 1; i < best_start; i++) {
        if (before != "") before = before ":"
        before = before words[i]
    }
    for (i = best_start + best_count; i <= 8; i++) {
        if (after != "") after = after ":"
        after = after words[i]
    }
    IPV6_NORMALIZED = before "::" after
    return 1
}

function parse_host(value, allow_ipv6) {
    if (value == "" || index(value, "[") || index(value, "]")) return 0
    if (index(value, ":") > 0) {
        if (!allow_ipv6 || !valid_ipv6(value)) return 0
        HOST_NORMALIZED = IPV6_NORMALIZED
        HOST_KIND = "ipv6"
        return 1
    }
    if (value ~ /^[0-9.]+$/) {
        if (!valid_ipv4(value)) return 0
        HOST_NORMALIZED = IPV4_NORMALIZED
        HOST_KIND = "ipv4"
        return 1
    }
    if (!valid_dns(value)) return 0
    HOST_NORMALIZED = DNS_NORMALIZED
    HOST_KIND = "dns"
    return 1
}

function wire_host(value, kind) {
    return kind == "ipv6" ? "[" value "]" : value
}

BEGIN {
    origin = ENVIRON["EM_PUBLIC_ORIGIN"]
    public_host = ENVIRON["EM_PUBLIC_HOST"]
    https_port = ENVIRON["EM_HTTPS_PORT"]
    if (origin == "" || public_host == "") exit 2
    if (origin ~ /[[:space:][:cntrl:]]/ || index(origin, "?") || index(origin, "#") || \
        index(origin, "@") || index(origin, "\\")) exit 2
    if (tolower(substr(origin, 1, 8)) != "https://") exit 2

    authority = substr(origin, 9)
    if (substr(authority, length(authority), 1) == "/") {
        authority = substr(authority, 1, length(authority) - 1)
        if (substr(authority, length(authority), 1) == "/") exit 2
    }
    if (authority == "" || index(authority, "/")) exit 2

    explicit_port = 0
    origin_port = "443"
    if (substr(authority, 1, 1) == "[") {
        close_bracket = index(authority, "]")
        if (close_bracket < 3 || index(substr(authority, 2, close_bracket - 2), "[") || \
            index(substr(authority, close_bracket + 1), "]")) exit 2
        origin_host = substr(authority, 2, close_bracket - 2)
        suffix = substr(authority, close_bracket + 1)
        if (suffix != "") {
            if (substr(suffix, 1, 1) != ":" || length(suffix) == 1) exit 2
            origin_port = substr(suffix, 2)
            explicit_port = 1
        }
        if (!valid_ipv6(origin_host)) exit 2
        origin_host_normalized = IPV6_NORMALIZED
        origin_host_kind = "ipv6"
    } else {
        if (index(authority, "[") || index(authority, "]")) exit 2
        colon_count = 0
        colon_at = 0
        for (i = 1; i <= length(authority); i++) {
            if (substr(authority, i, 1) == ":") {
                colon_count++
                colon_at = i
            }
        }
        if (colon_count > 1) exit 2
        if (colon_count == 1) {
            origin_host = substr(authority, 1, colon_at - 1)
            origin_port = substr(authority, colon_at + 1)
            explicit_port = 1
        } else {
            origin_host = authority
        }
        if (!parse_host(origin_host, 0)) exit 2
        origin_host_normalized = HOST_NORMALIZED
        origin_host_kind = HOST_KIND
    }
    if (explicit_port && !valid_port(origin_port)) exit 2

    if (https_port == "") https_port = "443"
    if (!valid_port(https_port)) exit 2
    if (origin_port + 0 != https_port + 0) exit 2

    if (!parse_host(public_host, 1)) exit 2
    if (HOST_KIND != origin_host_kind || HOST_NORMALIZED != origin_host_normalized) exit 2

    canonical_origin = "https://" wire_host(origin_host_normalized, origin_host_kind)
    if (explicit_port) canonical_origin = canonical_origin ":" origin_port
    approved_host = wire_host(HOST_NORMALIZED, HOST_KIND)
    origin_host_wire = wire_host(origin_host, origin_host_kind)
    public_host_wire = wire_host(public_host, HOST_KIND)
    printf "%s|%s|%s|%s\n", canonical_origin, approved_host, origin_host_wire, public_host_wire
}
' </dev/null) || fail_config

case "$render_values" in
    *'|'*) ;;
    *) fail_config ;;
esac
# Keep the caller's nginx arguments intact while unpacking validated values.
canonical_origin=${render_values%%|*}
render_tail=${render_values#*|}
case "$render_tail" in *'|'*) ;; *) fail_config ;; esac
approved_host=${render_tail%%|*}
render_tail=${render_tail#*|}
case "$render_tail" in *'|'*) ;; *) fail_config ;; esac
origin_host_wire=${render_tail%%|*}
public_host_wire=${render_tail#*|}
for render_value in "$canonical_origin" "$approved_host" "$origin_host_wire" "$public_host_wire"; do
    [ -n "$render_value" ] || fail_config
    case "$render_value" in
        *'|'*|*'&'*|*'\'*|*'$'*|*'`'*) fail_config ;;
    esac
done

if ! grep -F '@EM_PUBLIC_ORIGIN@' "$TEMPLATE" >/dev/null 2>&1 || \
    ! grep -F '/tmp/edge-host-map.conf' "$TEMPLATE" >/dev/null 2>&1; then
    fail_config
fi

umask 077
config_tmp=${CONFIG}.$$
host_map=/tmp/edge-host-map.conf
host_map_tmp=${host_map}.$$
trap 'rm -f "$config_tmp" "$host_map_tmp"' 0 HUP INT TERM
if ! printf '%s\n' "$origin_host_wire" "$public_host_wire" "$approved_host" | \
    LC_ALL=C awk -v approved="$approved_host" '
        NF && !seen[tolower($0)]++ {
            key = tolower($0)
            # Only these two words are directives in a two-argument map row.
            if (key == "default" || key == "include") key = "\\" key
            printf "\"%s\" \"%s\";\n", key, approved
        }
    ' > "$host_map_tmp"; then
    fail_config
fi
if [ ! -s "$host_map_tmp" ]; then
    fail_config
fi
mv "$host_map_tmp" "$host_map"
if ! sed \
    -e "s|@EM_PUBLIC_ORIGIN@|$canonical_origin|g" \
    "$TEMPLATE" > "$config_tmp"; then
    fail_config
fi
if grep -F '@EM_PUBLIC_' "$config_tmp" >/dev/null 2>&1; then
    fail_config
fi
mv "$config_tmp" "$CONFIG"
trap - 0 HUP INT TERM

if [ "${1-}" = "-t" ]; then
    exec nginx -c "$CONFIG" "$@"
fi
nginx -t -c "$CONFIG"
exec nginx -c "$CONFIG" "$@"
