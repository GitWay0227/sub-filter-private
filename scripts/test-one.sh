#!/usr/bin/env bash
# 单节点测活：经 mihomo API delay 测试；通过则输出节点名（\0 分隔）
# Usage: test-one.sh <node name>
enc=$(jq -rn --arg n "$1" '$n|@uri')
r=$(curl -s -m 6 -H "Authorization: Bearer $SECRET" \
  "http://127.0.0.1:19090/proxies/$enc/delay?url=https%3A%2F%2Fwww.gstatic.com%2Fgenerate_204&timeout=4000")
if printf '%s' "$r" | jq -e '.delay' >/dev/null 2>&1; then
  printf '%s\0' "$1"
fi
