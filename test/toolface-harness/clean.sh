#!/usr/bin/env bash
# harness 清理面：只删本 harness 自建的工作根（$HARNESS_ROOT）。
# root 身份断言 = realpath 前缀判定；「等于生产根 / 是生产根的祖先」两形态一律拒绝且不删。
# 不用 ignore_errors / 2>/dev/null 吞错：rm 失败即非 0 退出。
set -u
untildify() { case "$1" in "~"|"~/"*) printf '%s' "$HOME${1#\~}";; *) printf '%s' "$1";; esac; }
R="${HARNESS_ROOT:?HARNESS_ROOT 未设}"
WS=$(untildify "${HARNESS_WS:-$HOME/m}")
BASE=$(untildify "${HARNESS_BASE:-$WS/run/temp/toolface-harness}")
rr=$(realpath "$R") || { echo "REFUSE: realpath 失败 $R"; exit 2; }
base=$(realpath "$BASE") || { echo "REFUSE: realpath 失败 base"; exit 2; }
case "$rr" in
  "$base"|"$base"/*) : ;;
  *) echo "REFUSE: 工作根 $rr 不在临时基目录 $base 内 ⇒ 不删"; exit 2;;
esac
# 生产根清单：调用方注入（HARNESS_PROD_ROOTS，冒号分隔）∨ 现场发现（工作区根 + 其下含 .git 的一级子目录）
prod_list=()
if [ -n "${HARNESS_PROD_ROOTS:-}" ]; then
  IFS=':' read -r -a prod_list <<< "$HARNESS_PROD_ROOTS"
else
  prod_list=("$WS")
  for d in "$WS"/*/; do
    [ -d "${d}.git" ] && prod_list+=("$(untildify "${d%/}")")
  done
fi
for prod in "${prod_list[@]}"; do
  [ -n "$prod" ] || continue
  p=$(realpath "$(untildify "$prod")") || continue
  if [ "$rr" = "$p" ]; then echo "REFUSE: 工作根等于生产根 $p ⇒ 不删"; exit 2; fi
  case "$p" in "$rr"/*) echo "REFUSE: 生产根 $p 在工作根 $rr 内部（工作根是其祖先）⇒ 不删"; exit 2;; esac
done
echo "assert-ok: 将删除 $rr（临时基目录 $base 内；生产根清单 ${#prod_list[@]} 枚已过断言）"
# 留证：软链逐条打印身份（rm -rf 不跟随软链 ⇒ 生产资产零删除动作）
find "$rr" -maxdepth 2 -type l -printf 'symlink %p -> %l\n' | sed -n '1,60p'
rm -rf -- "$rr"
echo "cleaned: $rr（存在性 = $([ -e "$rr" ] && echo STILL-THERE || echo gone)）"
