#!/bin/sh
set -eu
CONFIG_FILE=${1:-}
LOG_FILE=${2:-/tmp/openclash.log}
[ -n "$CONFIG_FILE" ] && [ -f "$CONFIG_FILE" ] || { echo "WLOC remote: missing config" >&2; exit 1; }
WLOC_REMOTE_SERVER=${WLOC_REMOTE_SERVER:-192.168.50.2}
WLOC_ROUTER_IP=${WLOC_ROUTER_IP:-192.168.50.1}
WLOC_REMOTE_SERVER="$WLOC_REMOTE_SERVER" WLOC_ROUTER_IP="$WLOC_ROUTER_IP" WLOC_CONFIG_FILE="$CONFIG_FILE" ruby -ryaml -e '
  path = ENV.fetch("WLOC_CONFIG_FILE")
  server = ENV.fetch("WLOC_REMOTE_SERVER")
  router = ENV.fetch("WLOC_ROUTER_IP")
  value = YAML.safe_load(File.binread(path), permitted_classes: [], permitted_symbols: [], aliases: true)
  raise "generated config is not a mapping" unless value.is_a?(Hash)
  value["proxies"] ||= []
  value["rules"] ||= []
  name = "WLOC-ALL-MITM-REMOTE"
  value["proxies"].reject! { |p| p.is_a?(Hash) && p["name"].to_s == name }
  value["proxies"].unshift("name" => name, "type" => "http", "server" => server, "port" => 18081)
  value["rules"].reject! do |rule|
    text = rule.to_s
    text.include?(name) || text.include?("DOMAIN,gs-loc.apple.com") || text.include?("DOMAIN,gs-loc-cn.apple.com") || text.include?("SRC-IP-CIDR,#{server}/32") || text.include?("SRC-IP-CIDR,#{router}/32")
  end
  generated = [
    "SRC-IP-CIDR,#{server}/32,DIRECT",
    "SRC-IP-CIDR,#{router}/32,DIRECT",
    "AND,((DOMAIN,gs-loc.apple.com),(NETWORK,UDP)),REJECT",
    "AND,((DOMAIN,gs-loc-cn.apple.com),(NETWORK,UDP)),REJECT",
    "DOMAIN,gs-loc.apple.com,#{name}",
    "DOMAIN,gs-loc-cn.apple.com,#{name}"
  ]
  value["rules"] = generated + value["rules"]
  stat = File.stat(path)
  tmp = "#{path}.wloc-remote.#{$$}.tmp"
  begin
    File.open(tmp, "wb", stat.mode & 0o777) { |f| YAML.dump(value, f); f.flush; f.fsync }
    File.rename(tmp, path)
  ensure
    File.delete(tmp) if File.exist?(tmp)
  end
' 2>> "$LOG_FILE"
