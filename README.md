# 旧报数字档补扫核查 API

给旧报数字档补扫缺版用的核查服务。FastAPI + SQLite，`docker compose` 一键启动。

## 快速开始

```bash
docker compose up --build
# 打开 http://localhost:8000/docs 查看交互式接口文档
```

数据落在 compose 命名卷 `checker-data` 里（容器内 `/data/checker.db`）。
改数据库路径用环境变量 `NEWSPAPER_DB`。

本机直接跑：

```bash
pip install -r requirements-dev.txt
NEWSPAPER_DB=data/checker.db uvicorn app.main:app --reload
python -m pytest -q
```

## 登记与提交

每期的版号清单按**版序**登记，逐卷提交已扫帧的**原帧号**和**候选版号**；
认不出的帧候选留空即可。

```bash
# 首卷随全期清单一起提交
curl -X POST localhost:8000/issues/2026-11-22/volumes \
  -H 'content-type: application/json' -d '{
    "volume_code": "V01",
    "plates": ["1", "2", "3", "4"],
    "frames": [
      {"frame_no": 1001, "candidates": ["1"]},
      {"frame_no": 1002, "candidates": []},
      {"frame_no": 1004, "candidates": ["3", "4"]}
    ]
  }'

# 后续卷可省略 plates（沿用该期清单）
curl -X POST localhost:8000/issues/2026-11-22/volumes \
  -H 'content-type: application/json' -d '{
    "volume_code": "V02",
    "frames": [{"frame_no": 1, "candidates": ["2"]}]
  }'

# 单独登记 / 更换全期清单
curl -X PUT localhost:8000/issues/2026-11-22/plates \
  -H 'content-type: application/json' -d '{"plates": ["1","2","3","4","5"]}'
```

规则：

- 同一卷内相邻两帧拍同一版或下一版；
- 每卷首末已扫帧之间（含已扫帧）的跳号为漏扫，最多 500 帧；
- 换交一卷只重判当卷；清单变化时用新清单重判该期所有卷；
- 排不通或写库失败，**整次提交回滚**，沿用原数据（错误响应里
  `error.data_unchanged` 为 `true`）。

## 核查

```bash
curl localhost:8000/issues/2026-11-22/plates          # 按期查各版
curl localhost:8000/issues/2026-11-22/plates/3        # 单版
curl localhost:8000/issues/2026-11-22/volumes         # 各卷排法结果
```

按期汇总时，对该期所有卷的所有可行排法取并集：

| 状态 | 含义 | 附带线索 |
| --- | --- | --- |
| `scanned` 已扫 | 每种可行排法都有已扫帧拍到该版 | `guaranteed_by_volumes`、`scanned_frames` |
| `pending` 待核 | 只有部分排法有已扫帧拍到该版 | `possible_volumes_and_frames`（卷号 + 已扫帧号） |
| `missing` 缺扫 | 没有任何排法能由已扫帧拍到该版 | `possible_volumes_and_missed_frames`（卷号 + 漏扫帧号）；为空时 `hint` 提示另找胶片 |

## 实现要点

排法数随帧数指数增长（500 帧不可能枚举）。`app/coverage.py` 用位图
前向/后向可达性 DP 求每帧可落的版序集合，得到全部“∃ 排法”信息；
“∀ 排法”则对每个候选版再做一次“禁止所有已扫帧落在该版”的可行性检查，
复杂度 O(帧数 × 版数)，单卷 500 帧毫秒级完成。

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查（compose healthcheck 用） |
| GET | `/issues` | 已登记各期 |
| GET | `/issues/{issue_id}` | 该期清单与卷数 |
| PUT | `/issues/{issue_id}/plates` | 登记/更换全期清单并重判 |
| POST | `/issues/{issue_id}/volumes` | 提交一卷（可带清单） |
| GET | `/issues/{issue_id}/volumes` | 各卷排法结果 |
| GET | `/issues/{issue_id}/volumes/{volume_code}` | 单卷结果 |
| GET | `/issues/{issue_id}/plates` | 按期查各版 |
| GET | `/issues/{issue_id}/plates/{plate}` | 单版结论 |

错误统一为 `{"error": {"code": ..., "message": ...}}`；数据校验/排不通
返回 422，存储失败返回 503，均带 `data_unchanged: true`。
