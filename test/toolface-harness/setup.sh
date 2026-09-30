#!/usr/bin/env bash
# 建工作根 = 专用临时根 + 生产资产**软链** + harness 自建件拷入。
#
# 红线：本脚本只创建（mkdir/ln -s/cp），**绝不删除**任何东西（清理面 = clean.sh，那边有同一套
# root 身份断言）；工作根必须落在临时基目录内，且不得等于生产根、不得是生产根的祖先。
# 落仓件里不存任何机器专有值：工作区根 / 临时基目录 / 生产根清单都由 env 覆盖，缺省值现场发现。
#
# 用法：
#   ./setup.sh                     # 建 $HARNESS_BASE/r-<时间戳>，把根路径打到 stdout 最后一行
#   ./setup.sh <工作根路径>         # 指定根（仍须在临时基目录内）
# env：
#   HARNESS_WS    工作区根（缺省 $HOME/m；软链源 + 注入层位置的基准）
#   HARNESS_BASE  临时基目录（缺省 <HARNESS_WS>/run/temp/toolface-harness）
#   HARNESS_PROD_ROOTS  冒号分隔的生产根清单（缺省 = 现场发现：WS 根 + 其下含 .git 的一级子目录）
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
untildify() { case "$1" in "~"|"~/"*) printf '%s' "$HOME${1#\~}";; *) printf '%s' "$1";; esac; }
WS=$(untildify "${HARNESS_WS:-$HOME/m}")
BASE=$(untildify "${HARNESS_BASE:-$WS/run/temp/toolface-harness}")
R="${1:-$BASE/r-$(date +%Y%m%d-%H%M%S)}"

[ -d "$WS/bots" ] || { echo "REFUSE: $WS 不像 agent 工作区根（缺 bots/）⇒ 设 HARNESS_WS"; exit 2; }
[ -d "$WS/bots/profiles" ] && [ -d "$WS/bots/caps" ] || { echo "REFUSE: $WS/bots 下缺 profiles/ ∨ caps/"; exit 2; }

# ---- root 身份断言（与 clean.sh / drive.py:guard_root 同一套三条）----
rr=$(realpath -m "$R") || { echo "REFUSE: realpath 失败 $R"; exit 2; }
base=$(realpath -m "$BASE") || { echo "REFUSE: realpath 失败 base"; exit 2; }
case "$rr" in
  "$base"|"$base"/*) : ;;
  *) echo "REFUSE: 工作根 $rr 不在临时基目录 $base 内 ⇒ 不建"; exit 2;;
esac
prod_list=()
if [ -n "${HARNESS_PROD_ROOTS:-}" ]; then
  IFS=':' read -r -a prod_list <<< "$HARNESS_PROD_ROOTS"
else
  prod_list=("$WS")
  shopt -s nullglob
  for d in "$WS"/*/; do [ -d "${d}.git" ] && prod_list+=("${d%/}"); done
  shopt -u nullglob
fi
for prod in "${prod_list[@]}"; do
  [ -n "$prod" ] || continue
  p=$(realpath "$(untildify "$prod")") || continue
  if [ "$rr" = "$p" ]; then echo "REFUSE: 工作根等于生产根 $p ⇒ 不建"; exit 2; fi
  case "$p" in "$rr"/*) echo "REFUSE: 生产根 $p 在工作根 $rr 内部（工作根是其祖先）⇒ 不建"; exit 2;; esac
done
if [ -e "$rr" ] && [ -n "$(ls -A "$rr")" ]; then
  echo "REFUSE: $rr 已存在且非空 ⇒ 不覆盖（换个根名，∨ 先 clean.sh 清它）"; exit 2
fi
mkdir -p "$rr"/agents/task "$rr"/bots/profiles "$rr"/bots/caps "$rr"/out "$rr"/probe "$rr"/run/agentd "$rr"/run/logs

# ---- 生产资产软链：顶层逐项（真目录三名除外：bots/agents/run 由本 harness 自建）----
shopt -s nullglob
linked=0
for e in "$WS"/*; do
  n=$(basename "$e")
  case "$n" in bots|run|agents) continue;; esac
  ln -s "$e" "$rr/$n"; linked=$((linked + 1))
done
# bots/ 下：非 profiles|caps 的逐项软链（persona.py 的解析要用到 kb_index.py 等），
# profiles/ 与 caps/ 逐项软链生产资产，再拷入 harness 自建的 profile 与能力（名字带 harness- 前缀，
# 与生产资产不撞名；cp 不覆盖软链目标 ⇒ 生产面零写入）。
for e in "$WS"/bots/*; do
  n=$(basename "$e")
  case "$n" in profiles|caps) continue;; esac
  ln -s "$e" "$rr/bots/$n"
done
for e in "$WS"/bots/profiles/*; do ln -s "$e" "$rr/bots/profiles/$(basename "$e")"; done
for e in "$WS"/bots/caps/*; do ln -s "$e" "$rr/bots/caps/$(basename "$e")"; done
cp "$HERE"/assets/bots/profiles/*.json "$rr/bots/profiles/"
cp -R "$HERE"/assets/bots/caps/. "$rr/bots/caps/"
cp "$HERE"/probe/toolface-probe.ts "$rr/probe/"
shopt -u nullglob

echo "setup-ok: 工作根 $rr（顶层软链 $linked 枚；生产根清单 ${#prod_list[@]} 枚已过断言）"
echo "harness 自建面: $(ls "$rr/bots/profiles" | grep -c '^harness-') 枚 profile、$(ls -d "$rr/bots/caps"/harness-* | wc -l) 枚 cap、probe/toolface-probe.ts"
echo "下一步: HARNESS_ROOT=$rr python3 $HERE/drive.py <case> --mode direct|wrap …（跑法与 case 清单见 $HERE/README.md）"
echo "$rr"
