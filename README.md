# HTTP 路由覆盖冲突审计 API

用 **Python + FastAPI + Pydantic** 实现的路由覆盖冲突审计服务。

输入一组**按注册顺序**排列的 HTTP 路由（方法、路径模板、优先级），服务在统一的
URL 解码与规范化模型上模拟路由器匹配，判定：

- 同方法的两条路由**可达集合是否相交**；
- 谁会**遮蔽（shadow）**谁（优先级高者胜，同级时注册更早者胜）；
- 为每个已证实冲突构造一条**最短且稳定的示例路径（witness）**；
- 参数约束理论相交、但交集内无法构造见证时，标记为 **`unprovable`（不可证实）**；
- 列出**解析错误**（不中断整批分析）与**建议注册顺序**（更具体的模板排前面）。

另外提供两个配套端点：

- `POST /export-openapi`：把同一套路由配置转换为**可嵌入 OpenAPI 3.1 的
  `paths` 片段**（参数约束映射为 JSON Schema，末尾 `**` 映射为 `wildcard` 参数，
  自动生成稳定唯一的 `operationId`），不返回冲突统计；
- `POST /render-paths`：**反向生成**——提交带 `id` 的路由模板与参数值，校验
  `int` / `uuid` / `slug` 约束并做 RFC 3986 单段百分号编码，返回每条路由可直接
  调用的 canonical path（末尾 `**` 接收可为空的段数组）。

还有一对**发布/读取**端点，用于把校验通过的路由配置固化为可持久化的活动版本：

- `POST /publish`：对候选路由集同时运行冲突审计与 OpenAPI 导出，校验通过且
  `expected_version` 等于当前版本时**原子替换**活动路由并递增版本（CAS）；
- `GET /active`：从**同一快照**返回当前活动版本的路由、审计结果与 OpenAPI
  `paths`；状态落盘保存，容器重启后仍可读到已发布版本。初始版本为 0、路由为空。

最后是两个**只读**端点：

- `POST /precheck`：提交候选路由与一批请求样本（方法 + 绝对路径），在**固定的
  活动快照**上比较每个样本在「活动路由 vs 候选路由」下分别命中哪条路由——
  候选须先通过与 `/publish` 完全相同的校验，通过时逐样本返回规范化路径、两侧
  命中路由的原始下标 / id / 规范化模板以及是否改投；预检不修改活动配置。
- `POST /migrate-links`：为**已发布路由生成旧链接迁移清单**。提交按注册顺序
  排列的目标路由与一批旧链接（方法 + 绝对路径）；目标配置须先通过与
  `/publish` 完全相同的校验（否则整批拒绝），随后在**固定的活动快照**上为每条
  旧链接找旧命中，按**同方法下唯一的非空显式 id** 对应到目标路由，提取旧模板
  的命名参数与末尾 `**` 各段填入目标模板生成 canonical path，并再按整套目标
  路由重新选胜方（只有胜方恰是对应目标路由才返回路径）；同样不修改活动配置。

结果按「胜方优先级 → 方法 → 胜方规范化路径 → 原始下标」稳定排序。

---

## 一条命令启动

```bash
docker compose up --build
```

启动后：

| 用途 | 地址 |
| --- | --- |
| **API 基础地址 / 端口** | **http://localhost:8000** |
| 健康检查 | http://localhost:8000/healthz |
| 交互式文档（Swagger UI） | http://localhost:8000/docs |
| OpenAPI 模式 | http://localhost:8000/openapi.json |

> 容器内监听 `0.0.0.0:8000`，通过 Compose 映射到宿主机的 **8000** 端口。
> 如端口被占用，修改 `docker-compose.yml` 中 `"8000:8000"` 的左侧值即可。

已发布的活动版本持久化在命名卷 `route-audit-data`（容器内 `/data/active.json`，
由环境变量 `ROUTE_AUDIT_STATE_FILE` 指定）中：`docker compose down` 后重新
`up`、甚至重新 `--build`，已发布版本都会保留。只有 `docker compose down -v`
删除卷才会清空已发布状态（回到版本 0、空路由）。

停止：另开终端执行 `docker compose down`（或在启动终端按 `Ctrl+C`）。

### 不用 Docker 的本地运行方式

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 快速试用

```bash
curl -s -X POST http://localhost:8000/analyze \
  -H 'Content-Type: application/json' \
  -d '{
    "routes": [
      {"method": "GET", "path": "/api/**",                "priority": 0,  "id": "catchall"},
      {"method": "GET", "path": "/api/users/{id:int}",    "priority": 5,  "id": "user"},
      {"method": "GET", "path": "/api/users/me",          "priority": 10, "id": "me"}
    ]
  }'
```

响应（节选）：

```json
{
  "conflicts": [
    {
      "winner_id": "me", "loser_id": "catchall",
      "kind": "partial_overlap", "status": "proven",
      "witness_path": "/api/users/me"
    },
    {
      "winner_id": "user", "loser_id": "catchall",
      "kind": "partial_overlap", "status": "proven",
      "witness_path": "/api/users/1"
    }
  ]
}
```

### 反向生成可调用 URL

```bash
curl -s -X POST http://localhost:8000/render-paths \
  -H 'Content-Type: application/json' \
  -d '{
    "routes": [
      {"id": "user-posts",
       "template": "/users/{id:int}/posts/{slug:slug}",
       "parameters": {"id": 42, "slug": "hello-world"}},
      {"id": "assets",
       "template": "/assets/**",
       "wildcard": ["img/lo go", "café.png"]},
      {"id": "bad",
       "template": "/users/{id:int}",
       "parameters": {"id": "abc"}}
    ]
  }'
```

响应（结果与输入等长、同序；单项失败不中止批处理）：

```json
{
  "results": [
    {"index": 0, "id": "user-posts",
     "template": "/users/{id:int}/posts/{slug:slug}",
     "normalized_template": "/users/{id:int}/posts/{slug:slug}",
     "canonical_path": "/users/42/posts/hello-world", "errors": []},
    {"index": 1, "id": "assets",
     "template": "/assets/**", "normalized_template": "/assets/**",
     "canonical_path": "/assets/img%2Flo%20go/caf%C3%A9.png", "errors": []},
    {"index": 2, "id": "bad",
     "template": "/users/{id:int}", "normalized_template": "/users/{id:int}",
     "canonical_path": null,
     "errors": [
       {"code": "invalid_parameter", "field": "id",
        "message": "value for 'id' does not satisfy int constraint"}
     ]}
  ]
}
```

---

## 路径模板语法

模板必须以 `/` 开头，由 `/` 分隔的段构成：

| 段形式 | 含义 | 示例 |
| --- | --- | --- |
| 静态段 | 字面文本，注册时做规范化 | `/users/profile` |
| `{name}` | 通用参数，匹配任意**非空、不含 `/`** 的单段 | `/files/{name}` |
| `{name:int}` | 非负整数：`0` 或 `[1-9][0-9]*`（无前导零） | `/users/{id:int}` |
| `{name:uuid}` | 8-4-4-4-12 十六进制（大小写均可） | `/users/{id:uuid}` |
| `{name:slug}` | 小写字母数字与中划线：`[a-z0-9]+(-[a-z0-9]+)*` | `/posts/{slug:slug}` |
| 末尾 `**` | 只能作为**最后一段**，匹配**零个或多个**后续段 | `/assets/**`、`/**` |

规则细节：

- 参数名必须是 `[A-Za-z_][A-Za-z0-9_]*`，同一模板内不可重复。
- 花括号必须包裹**整段**：`/a/{id:int}` 合法，`/a/pre{id}` 非法。
- `**` 只能出现在末尾；单独的 `*` 不支持。
- 内部空段（`//`）非法；尾随斜杠会被规范化移除（产生 warning）。
- `int` 与 `uuid` 不相交；二者都是 `slug` 的真子集；`slug` 是通用参数的真子集。

### 通配 `**` 的语义

`**` 匹配**零个或多个**段，因此：

- `/a/**` 同时覆盖 `/a`、`/a/b`、`/a/b/c`；
- `/a/**` 与根 `/` **不**相交（前缀 `/a` 无法越过）；
- `/a/**` 与 `/a/b/**` 在 `/a/b` 处相交；
- `/**` 覆盖一切路径（含根）。

---

## URL 解码与规范化规则（审计模型）

审计器在一个**明确定义、幂等**的规范化路径上判定匹配，模板与请求使用同一套规则：

1. **安全百分号解码**
   - 非保留字符（RFC 3986 unreserved：`A-Z a-z 0-9 - . _ ~`）被解码：
     `/%41` → `/A`，`/%7e` → `/~`；
   - **保留字符不解码**：`%2F` 不会变成段分隔符（`/a%2Fb` 与 `/a/b` 不相交），
     `%3F`、`%23` 等同理；十六进制位统一为大写；
   - 非法转义（`%zz`、`%2`、孤立的 `%`）与控制字符（含 `%00`、`%7F`）→ 解析错误。
2. **点段移除**：按 RFC 3986 §5.2.4 处理 `.` / `..`；`..` 不可越过根
   （`/../a` → `/a`）。因此 `/x/./y`、`/z/%2e/w` 会与去点后的模板撞车。
3. **无尾随斜杠语义**：尾随 `/` 被移除（`/a/b/` ≡ `/a/b`）；根路径记为零段。
4. **内部空段保留**：`/a//b` 的空段保留在规范化结果中，静态段和参数段
   （参数至少匹配一个非空字符）都无法匹配它，只有 `**` 能吸收。

构造出的 witness 会经过一次**完整的再规范化往返校验**：重新规范化后必须仍同时
命中两条路由，否则降级为 `unprovable`。

---

## API 参考

### `POST /analyze`

请求体：

```json
{
  "routes": [
    {
      "method": "GET",
      "path": "/users/{id:int}",
      "priority": 0,
      "id": "optional-caller-id"
    }
  ]
}
```

- `routes` **按注册顺序**排列（数组顺序即注册顺序）。
- 字段也接受别名 `template`（等价于 `path`）。
- `method`：大小写不敏感，允许
  `GET POST PUT PATCH DELETE HEAD OPTIONS TRACE CONNECT`；其它方法记入 `parse_errors`。
- `priority`：整数，**数值越大越先匹配**；缺省 `0`；相同优先级按注册顺序决胜。
- `id`：可选；缺省时生成 `route-<数组下标>`。未知字段会被忽略。
- 信封级别校验失败（如缺少 `path`）返回 **422**；单条路由的问题不影响其它路由，
  统一以 **200 + `parse_errors`** 返回。

响应字段：

| 字段 | 说明 |
| --- | --- |
| `routes[]` | 所有有效路由：规范化后的模板、固定段数、是否含通配、规范化 warnings |
| `parse_errors[]` | 解析失败的路由：输入下标、方法/模板、错误原因 |
| `conflicts[]` | 成对冲突报告（见下） |
| `suggested_order[]` | 按方法分组的建议注册顺序及每条路由的移动量 |
| `summary` | 总数、有效数、冲突数、完全遮蔽/重复/部分相交计数、不可证实数、被遮蔽路由数 |

`conflicts[]` 每项：

| 字段 | 说明 |
| --- | --- |
| `winner_*` / `loser_*` | 胜方与负方的下标、id、模板、优先级 |
| `kind` | `duplicate`（可达集合完全相同）、`full_shadow`（负方被胜方完全遮蔽）、`partial_overlap`（仅部分相交） |
| `status` | `proven`（已构造 witness）或 `unprovable`（约束相交但无法构造见证） |
| `witness_path` | 同时命中双方的**最短稳定**示例路径；`unprovable` 时为 `null` |
| `reason` | `unprovable` 时的原因 |

### 遮蔽决胜规则

1. `priority` 高者胜；
2. 优先级相同时，注册顺序更早者胜。

注意：胜方只代表“谁会先抢走这个请求”。`full_shadow`/`duplicate` 表示负方**永远
没有机会**被命中；`partial_overlap` 表示双方各有独占路径，仅在 witness 处撞车。

### 建议顺序

`suggested_order` 不改变优先级语义，只给出“若希望更具体模板先生效”的稳定排列：

1. 不含 `**` 的优先；
2. 固定段更长的优先；
3. 静态段更多、且静态/参数区分出现在更靠前位置的优先；
4. 以上都相同时保持原注册顺序。

`move = 原位置 − 新位置`：正数表示前移，负数表示后移，`0` 表示不变。

### `GET /healthz`

返回 `{"status": "ok"}`，供容器健康检查使用。

### `POST /export-openapi`

把同一套路由配置转换为**可嵌入 OpenAPI 3.1 文档的 `paths` 片段**。只做结构转换，
不返回任何冲突统计（冲突审计请用 `POST /analyze`）。

请求体与 `/analyze` 相同：

```json
{
  "routes": [
    {"method": "GET", "path": "/users/{id:int}", "id": "getUser"},
    {"method": "GET", "path": "/assets/**"}
  ]
}
```

响应（`paths` 可整体拷入 OpenAPI 3.1 文档的 `paths:` 下；每个操作附带最小
`responses` 占位，保证片段本身合法）：

```json
{
  "paths": {
    "/assets/{wildcard}": {
      "get": {
        "operationId": "get_assets_wildcard",
        "parameters": [
          {"name": "wildcard", "in": "path", "required": true,
           "schema": {"type": "string"}, "x-route-wildcard": true}
        ],
        "responses": {"default": {"description": "Default response"}}
      }
    },
    "/users/{id}": {
      "get": {
        "operationId": "getUser",
        "parameters": [
          {"name": "id", "in": "path", "required": true,
           "schema": {"type": "integer", "minimum": 0}}
        ],
        "responses": {"default": {"description": "Default response"}}
      }
    }
  },
  "parse_errors": []
}
```

#### 转换规则

| 模板段 | OpenAPI 表示 |
| --- | --- |
| 静态段 | 按规范化结果原样保留 |
| `{name}` | path 参数 `name`，schema `{"type": "string", "minLength": 1}` |
| `{name:int}` | path 参数 `name`，schema `{"type": "integer", "minimum": 0}` |
| `{name:uuid}` | path 参数 `name`，schema `{"type": "string", "format": "uuid"}` |
| `{name:slug}` | path 参数 `name`，schema `{"type": "string", "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$"}` |
| 末尾 `**` | path 参数 `wildcard`（`{"type": "string"}`），附 `"x-route-wildcard": true` |

- 路径模板与参数使用与审计相同的规范化结果（点段、尾随斜杠、百分号转义先被规范化）。
- 约束不进入路径模板，只体现在参数 schema 上；因此 `/x/{id:int}` 与 `/x/{id:uuid}`
  会导出为同一路径 `/x/{id}`。
- 模板内若已有名为 `wildcard` 的参数，末尾 `**` 无法再映射为同名参数，该路由记入
  `parse_errors`。

#### operationId 规则

1. **优先使用显式 `id`**；
2. 缺少 `id` 时由**方法 + 规范化路径**生成，如 `GET /users/{id}` → `get_users_id`、
   `GET /` → `get_root`、`GET /assets/**` → `get_assets_wildcard`；
3. 重名时追加**重复序号**（`-2`、`-3` …）；序号在「规范化路径 → 方法」排序后的
   稳定顺序上分配，与输入顺序无关，保证结果稳定且唯一。

#### 重复项处理

同方法且导出路径相同的重复项**不会静默覆盖**：保留先注册者，后续重复项记入
`parse_errors`，错误信息带双方原始下标。不同方法共用同一路径是合法的，会合并到
同一个 path 项下。

片段按键排序：先按规范化路径，同路径内按方法名排序。

### `POST /render-paths`

按路由模板**反向生成**可调用 URL。提交一批带参路由，服务逐条解析规范化模板、
校验参数约束并做百分号编码，返回每条路由的 canonical path。**单项失败不中止
批处理**：每条结果独立携带 `errors`；响应始终 200，`results` 与输入等长、同序。

请求体：

```json
{
  "routes": [
    {
      "id": "optional-caller-id",
      "template": "/users/{id:int}/posts/{slug:slug}",
      "parameters": {"id": 42, "slug": "hello-world"},
      "wildcard": null
    }
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `id` | 可选标识；在响应中原样回填，便于对齐结果（缺失为 `null`） |
| `template` | 路径模板（也接受别名 `path`）；语法与 `/analyze` 完全一致 |
| `parameters` | 参数名 → 值的对象；值为字符串，或可精确表示为整数的 JSON 数字 |
| `wildcard` | 末尾 `**` 的段数组，**可为空数组或缺省**（匹配零段）；模板不含 `**` 时只允许空/缺省 |

- 参数值按**模板声明**提供：缺少按模板参数顺序逐项报 `missing_parameter`，
  额外的按键名排序逐项报 `extra_parameter`。
- 取值约束与模板类型一致：`int`（`0` 或无前导零的正整数）、`uuid`（8-4-4-4-12
  十六进制）、`slug`（小写字母数字与中划线）、`{name}`（任意非空单段）；
  不合规逐项报 `invalid_parameter`。布尔、浮点、`null`、数组、对象都不是合法段值。
- 空段、ASCII 控制字符、字面 `.` / `..` 段（会被点段规范化吞掉）一律拒绝。
- 通配数组的逐段规则与参数相同，非法段报 `invalid_wildcard`（`field` 为
  `wildcard[<下标>]`）；非通配模板提供非空数组报 `wildcard_not_allowed`。
- 模板本身解析失败（非法语法、非法百分号转义等）报 `template_invalid`，该条
  `canonical_path` 与 `normalized_template` 为 `null`。
- 任一条目存在错误时，该条 `canonical_path` 为 `null`，但不影响其它条目。
- 信封级校验失败（如缺少 `template`）返回 **422**，与 `/analyze` 一致。

#### 百分号编码规则

每个参数值按 **RFC 3986 单段**编码（等价于对段做 `quote(value, safe="")`）：

- 只有 unreserved 字符（`A-Z a-z 0-9 - . _ ~`）保持原样，其余全部转义；
- 因此值中的 `/` 变为 `%2F`、空格变为 `%20`、`%` 变为 `%25`、`?` 变为 `%3F`，
  一个参数**永远只占一个路径段**；
- 非 ASCII 字符按 UTF-8 字节转义（`café` → `caf%C3%A9`）；
- 末尾 `**` 的每个数组元素独立做同样的单段编码后逐段拼上；
- 静态段使用与审计相同的规范化字面文本（不解码保留字符），生成结果会经过一次
  完整的规范化往返校验，确保返回的是稳定 canonical path。

`results[]` 每项：

| 字段 | 说明 |
| --- | --- |
| `index` | 在输入数组中的原始下标 |
| `id` | 原样回填的调用方标识（可能为 `null`） |
| `template` / `normalized_template` | 原始模板 / 解析规范化后的模板（模板非法时后者为 `null`） |
| `canonical_path` | 反向生成的可调用路径；有任何错误时为 `null` |
| `errors[]` | 逐项错误：`code`、`field`（参数名或 `wildcard[i]`，模板级为 `null`）、`message` |

错误码一览：`template_invalid`、`missing_parameter`、`extra_parameter`、
`invalid_parameter`、`invalid_wildcard`、`wildcard_not_allowed`、`not_canonical`。

### `POST /publish`

把一组候选路由**发布为新的活动版本**。服务对同一候选集依次运行与
`/analyze`、`/export-openapi` 完全相同的冲突审计与 OpenAPI 导出：

- 任一侧报告**解析错误**、导出侧报告**路径重复**，或审计结果含
  **`full_shadow`**（有路由被完全遮蔽）→ 拒绝发布，活动版本保持不变；
- 校验通过且 `expected_version` **等于当前版本** → 原子替换活动路由并把版本
  号加一（compare-and-swap）；两个并发发布使用同一预期版本时只有一个成功，
  另一个收到 `version_conflict` 与当前版本号，不会覆盖已发布内容。

请求体：

```json
{
  "expected_version": 0,
  "routes": [
    {"method": "GET", "path": "/api/**",                             "id": "catchall"},
    {"method": "GET", "path": "/api/users/{id:int}", "priority": 5,  "id": "user"}
  ]
}
```

- `routes` 与 `/analyze` 完全相同（按注册顺序排列）。
- `expected_version`：调用方期望的当前活动版本号；初始版本为 **0**，因此首次
  发布必须传 `0`。缺少该字段或字段类型非法返回 **422**。

```bash
curl -s -X POST http://localhost:8000/publish \
  -H 'Content-Type: application/json' \
  -d '{
    "expected_version": 0,
    "routes": [
      {"method": "GET", "path": "/api/**",                            "id": "catchall"},
      {"method": "GET", "path": "/api/users/{id:int}", "priority": 5, "id": "user"}
    ]
  }'
```

发布成功（版本递增为 1）：

```json
{"status": "published", "version": 1, "parse_errors": [], "conflicts": []}
```

校验失败（`status: "rejected"`，活动版本不变；`parse_errors` / `conflicts`
携带**原始下标**定位问题路由）：

```json
{
  "status": "rejected",
  "version": 1,
  "parse_errors": [
    {"index": 1, "id": null, "method": "GET", "template": "/x/{id:uuid}",
     "error": "duplicate route: GET /x/{id} already exported from index 0; keeping the first registration"}
  ],
  "conflicts": []
}
```

版本冲突（`status: "version_conflict"`，`version` 为当前版本，已发布内容不变）：

```json
{"status": "version_conflict", "version": 1, "parse_errors": [], "conflicts": []}
```

| 字段 | 说明 |
| --- | --- |
| `status` | `published` / `rejected` / `version_conflict` |
| `version` | 操作完成后的当前活动版本号（拒绝/冲突时保持不变） |
| `parse_errors[]` | rejected 时：审计与导出两侧报告的解析错误与重复导出路径（含原始下标） |
| `conflicts[]` | rejected 时：审计发现的 `full_shadow` 冲突（含双方原始下标与 witness） |

### `GET /active`

读取当前活动版本的**完整快照**：路由、冲突审计结果与 OpenAPI `paths` 均来自
同一次发布，绝不把旧路由与新导出片段拼接在一起。无需请求体。

```bash
curl -s http://localhost:8000/active
```

响应（首次发布前为版本 0、空路由）：

```json
{
  "version": 1,
  "routes": [
    {"method": "GET", "path": "/api/**", "priority": 0, "id": "catchall"},
    {"method": "GET", "path": "/api/users/{id:int}", "priority": 5, "id": "user"}
  ],
  "audit": {
    "routes": ["…同 POST /analyze 的响应结构…"],
    "conflicts": [
      {"winner_id": "user", "loser_id": "catchall",
       "kind": "partial_overlap", "status": "proven",
       "witness_path": "/api/users/1"}
    ],
    "summary": {"total": 2, "valid": 2, "conflicts": 1}
  },
  "openapi_paths": {
    "/api/users/{id}": {"get": {"operationId": "user"}},
    "/api/{wildcard}": {"get": {"operationId": "catchall"}}
  }
}
```

| 字段 | 说明 |
| --- | --- |
| `version` | 当前活动版本号；初始为 0 |
| `routes[]` | 发布时提交的路由（按注册顺序，与 `POST /publish` 请求同构） |
| `audit` | 发布时对同一路由集运行的冲突审计结果（结构同 `POST /analyze` 响应） |
| `openapi_paths` | 发布时生成的 OpenAPI 3.1 `paths` 片段（结构同 `POST /export-openapi` 的 `paths`） |

快照在发布时整体计算并原子落盘（默认 `data/active.json`，可用环境变量
`ROUTE_AUDIT_STATE_FILE` 覆盖；Compose 部署下为卷内的 `/data/active.json`），
进程/容器重启后从同一文件恢复，读取永远返回同一版本的完整快照。

### `POST /precheck`

**只读**预检：比较一批请求样本在「当前活动路由」与「候选路由」下分别会命中
哪条路由。端点**先固定当前活动快照**（版本 + 路由），整个比较都基于这同一份
快照；预检本身不修改活动配置，之后 `/publish` 对过期的 `expected_version`
仍按原规则拒绝。

处理流程：

1. 候选路由集先通过与 `/publish` **完全相同**的校验：任一解析错误、导出路径
   重复或 `full_shadow` → `status: "rejected"`，返回含原始下标的
   `parse_errors` / `conflicts`，**整批不做归属比较**（`results` 为空）；
2. 校验通过时逐样本匹配：样本路径按与审计相同的解码/规范化规则处理
   （`%2F` 不会变成段分隔符），再分别在两套路由中按「优先级降序、同级注册
   先后」选出**唯一命中**；
3. 路由身份按**方法、规范化模板、显式 id** 判定：同一条路由在候选中移动了
   注册下标、或调整了优先级，都不算改投。

请求体（`routes` 与 `/publish` 同构、按注册顺序排列；`samples` 为方法 +
绝对路径的请求样本）：

```json
{
  "routes": [
    {"method": "GET", "path": "/users/{id:int}", "priority": 5, "id": "user"},
    {"method": "GET", "path": "/assets/**", "id": "assets"},
    {"method": "GET", "path": "/assets/logo.png", "priority": 10, "id": "logo"}
  ],
  "samples": [
    {"method": "GET", "path": "/users/42"},
    {"method": "GET", "path": "/assets/logo.png"},
    {"method": "GET", "path": "/assets/css/site.css"},
    {"method": "POST", "path": "/users/42"}
  ]
}
```

```bash
curl -s -X POST http://localhost:8000/precheck \
  -H 'Content-Type: application/json' \
  -d '{
    "routes": [
      {"method": "GET", "path": "/users/{id:int}", "priority": 5, "id": "user"},
      {"method": "GET", "path": "/assets/**", "id": "assets"},
      {"method": "GET", "path": "/assets/logo.png", "priority": 10, "id": "logo"}
    ],
    "samples": [
      {"method": "GET", "path": "/users/42"},
      {"method": "GET", "path": "/assets/logo.png"},
      {"method": "GET", "path": "/assets/css/site.css"},
      {"method": "POST", "path": "/users/42"}
    ]
  }'
```

响应（`results` 与 `samples` 等长、同序；`version` 为所固定快照的版本；
假设活动版本 1 中只有 `user` 与 `assets` 两条路由）：

```json
{
  "status": "compared",
  "version": 1,
  "parse_errors": [],
  "conflicts": [],
  "results": [
    {"index": 0, "method": "GET", "path": "/users/42",
     "normalized_path": "/users/42",
     "active": {"index": 0, "id": "user",
                "normalized_template": "/users/{id:int}"},
     "candidate": {"index": 0, "id": "user",
                   "normalized_template": "/users/{id:int}"},
     "changed": false, "errors": []},
    {"index": 1, "method": "GET", "path": "/assets/logo.png",
     "normalized_path": "/assets/logo.png",
     "active": {"index": 1, "id": "assets",
                "normalized_template": "/assets/**"},
     "candidate": {"index": 2, "id": "logo",
                   "normalized_template": "/assets/logo.png"},
     "changed": true, "errors": []},
    {"index": 2, "method": "GET", "path": "/assets/css/site.css",
     "normalized_path": "/assets/css/site.css",
     "active": {"index": 1, "id": "assets",
                "normalized_template": "/assets/**"},
     "candidate": {"index": 1, "id": "assets",
                   "normalized_template": "/assets/**"},
     "changed": false, "errors": []},
    {"index": 3, "method": "POST", "path": "/users/42",
     "normalized_path": "/users/42",
     "active": null, "candidate": null,
     "changed": false, "errors": []}
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `status` | `compared`（候选通过校验并完成比较）/ `rejected`（候选未通过 `/publish` 同款校验，整批不比较） |
| `version` | 本次比较所固定的活动快照版本号 |
| `parse_errors[]` / `conflicts[]` | rejected 时：候选路由的解析错误/重复导出路径与 `full_shadow` 冲突（含原始下标），含义同 `/publish` |
| `results[]` | compared 时逐样本的归属比较，与 `samples` 等长、同序 |

`results[]` 每项：

| 字段 | 说明 |
| --- | --- |
| `index` | 样本在输入数组中的原始下标 |
| `method` / `path` | 大写规范化后的方法 / 原始路径回显 |
| `normalized_path` | 规范化后的样本路径；路径非法时为 `null` |
| `active` / `candidate` | 两侧的唯一命中路由 `{index, id, normalized_template}`（`index` 为各自路由数组中的原始下标）；**无命中明确记为 `null`** |
| `changed` | 是否改投；样本存在错误时为 `null` |
| `errors[]` | 样本级错误（非法方法/路径），逐项报告、不影响其余样本 |

`changed` 判定规则：

- 两侧命中路由的（方法、规范化模板、显式 id）完全一致 → `false`
  （同一条路由在候选中移动下标不算改投）；
- 仅一侧有命中 → `true`；两侧都无命中 → `false`；
- 命中路由的身份不一致 → `true`。

非法样本（不支持的方法、非 `/` 开头的路径、非法百分号转义等）逐项记入
`errors`，该样本的 `active` / `candidate` / `changed` 为 `null`，其余样本
继续正常比较；`%2F` 在样本中始终是单段字面量，不会变成段分隔符。
信封级校验失败（如缺少 `samples`）返回 **422**，与其它端点一致。

### `POST /migrate-links`

**只读**的旧链接迁移清单：为已经发布的路由生成一批旧链接在**目标路由集**下的
新 canonical path。端点**先固定当前活动快照**（版本 + 路由），整个迁移都基于
这同一份快照；迁移本身不修改活动配置。

处理流程：

1. **目标集校验（整批闸门）**：目标路由（`routes`，按注册顺序排列，结构与
   `/publish` 相同）必须先通过与 `/publish` **完全相同**的校验：任一解析错误、
   导出路径重复或 `full_shadow` → `status: "rejected"`，返回含原始下标的
   `parse_errors` / `conflicts`，**整批不迁移**（`results` 为空）；
2. **找旧命中**：每条旧链接（方法 + 绝对路径）先做与审计相同的解码/规范化，
   再在**活动路由**中按既有规则（优先级降序、同级注册先后）选出唯一命中；
3. **按显式 id 对应**：旧命中路由与目标路由以**同方法下唯一的非空显式
   `id`** 对应——旧命中缺少非空 `id`、该 `id` 在活动或目标同方法路由中不唯一
   （歧义）、或目标侧没有该 `id`，该链接都无法迁移；
4. **提取与回填**：提取旧模板的**命名参数**实际段值与末尾 `**` 吸收的各段，
   按**同名**填入目标模板：
   - 目标的每个命名参数都必须能从旧的**同名命名参数**取到值（旧 `**` 的段不
     会顶替目标参数；旧路由多余的参数会被忽略）；
   - 目标含末尾 `**` 时，旧 `**` 的各段整体搬入；目标不含 `**` 时旧尾部必须
     为空（零段）；
   - 每个段值还必须满足目标参数声明的约束（`int` / `uuid` / `slug` / 通用）；
5. **重新选胜方**：生成的 canonical path 再按**整套目标路由**重新匹配，只有
   胜方**恰好是**第 3 步对应的那条目标路由才返回路径，避免新路径被另一条目标
   路由（如新增的更具体静态路由）截获。

任一链接失败（旧无命中、id 缺失/歧义、目标缺失、参数无法完整对应、约束不满足、
被截获等）只在该链接的 `errors` 中逐项说明，**不影响其它链接**。

请求体（`routes` 与 `/publish` 同构、按注册顺序；`links` 为旧链接）：

```json
{
  "routes": [
    {"method": "GET", "path": "/api/users/{id:int}", "priority": 5, "id": "user"},
    {"method": "GET", "path": "/api/assets/**", "id": "assets"}
  ],
  "links": [
    {"method": "GET", "path": "/v1/users/42"},
    {"method": "GET", "path": "/v1/assets/img/logo.png"}
  ]
}
```

```bash
# 先发布一套旧路由（版本 1）
curl -s -X POST http://localhost:8000/publish \
  -H 'Content-Type: application/json' \
  -d '{
    "expected_version": 0,
    "routes": [
      {"method": "GET", "path": "/v1/users/{id:int}", "priority": 5, "id": "user"},
      {"method": "GET", "path": "/v1/assets/**", "id": "assets"}
    ]
  }'

# 再生成迁移清单
curl -s -X POST http://localhost:8000/migrate-links \
  -H 'Content-Type: application/json' \
  -d '{
    "routes": [
      {"method": "GET", "path": "/api/users/{id:int}", "priority": 5, "id": "user"},
      {"method": "GET", "path": "/api/assets/**", "id": "assets"}
    ],
    "links": [
      {"method": "GET", "path": "/v1/users/42"},
      {"method": "GET", "path": "/v1/assets/img/logo.png"}
    ]
  }'
```

响应（`results` 与 `links` 等长、同序；`version` 为所固定快照的版本）：

```json
{
  "status": "migrated",
  "version": 1,
  "parse_errors": [],
  "conflicts": [],
  "results": [
    {"index": 0, "method": "GET", "path": "/v1/users/42",
     "active_version": 1, "normalized_path": "/v1/users/42",
     "active": {"index": 0, "id": "user",
                "normalized_template": "/v1/users/{id:int}"},
     "target": {"index": 0, "id": "user",
                "normalized_template": "/api/users/{id:int}"},
     "canonical_path": "/api/users/42", "errors": []},
    {"index": 1, "method": "GET", "path": "/v1/assets/img/logo.png",
     "active_version": 1, "normalized_path": "/v1/assets/img/logo.png",
     "active": {"index": 1, "id": "assets",
                "normalized_template": "/v1/assets/**"},
     "target": {"index": 1, "id": "assets",
                "normalized_template": "/api/assets/**"},
     "canonical_path": "/api/assets/img/logo.png", "errors": []}
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `status` | `migrated`（目标通过校验并逐项迁移）/ `rejected`（目标未通过 `/publish` 同款校验，整批拒绝） |
| `version` | 本次迁移所固定的活动快照版本号 |
| `parse_errors[]` / `conflicts[]` | rejected 时：目标路由的解析错误/重复导出路径与 `full_shadow` 冲突（含原始下标） |
| `results[]` | migrated 时逐链接的迁移结果，与 `links` 等长、同序 |

`results[]` 每项：

| 字段 | 说明 |
| --- | --- |
| `index` | 链接在输入数组中的原始下标 |
| `method` / `path` | 大写规范化后的方法 / 原始旧链接回显 |
| `active_version` | 本次迁移所固定的活动快照版本号 |
| `normalized_path` | 规范化后的旧链接路径；路径非法时为 `null` |
| `active` | 旧链接在活动快照上的命中路由 `{index, id, normalized_template}`；无命中为 `null` |
| `target` | 按显式 id 对应的目标路由；无法定位（缺失/歧义）时为 `null` |
| `canonical_path` | 回填目标模板并通过胜方复核的 canonical path；失败时为 `null` |
| `errors[]` | 逐项失败原因 `{code, field, message}`；非空时 `canonical_path` 为 `null` |

错误码一览：

| code | 含义 |
| --- | --- |
| `invalid_method` | 旧链接使用了不支持的 HTTP 方法 |
| `invalid_path` | 旧链接不是以 `/` 开头的绝对路径，或含非法百分号转义等 |
| `no_active_match` | 活动路由中没有任何路由命中该旧链接 |
| `active_id_missing` | 旧命中路由缺少非空显式 `id`（生成的 `route-<下标>` 不算） |
| `ambiguous_id` | 该显式 `id` 在活动或目标的**同方法**路由中不唯一 |
| `target_missing` | 目标路由中没有携带该显式 `id` 的同方法路由 |
| `parameter_mismatch` | 目标命名参数在旧模板中没有同名参数，或目标无 `**` 而旧尾部非空 |
| `constraint_violated` | 提取出的段值不满足目标参数约束（或空段无法渲染） |
| `target_not_winner` | 生成路径在整套目标路由上被**另一条**路由截获 |
| `not_canonical` | 生成结果未通过完整规范化往返校验 |

百分号编码与 `/render-paths` 的**单段**语义完全一致：`%2F` 始终是段内字面量，
既不会被拆成段分隔符，也不会被重复编码（`%2F` → `%2F`、`%252F` → `%252F`）；
非 ASCII 字符按 UTF-8 字节转义。非法链接（不支持的方法、相对路径、非法转义等）
逐项记入 `errors`，不中止批处理。信封级校验失败（缺少 `routes` / `links` 等）
返回 **422**，与其它端点一致。

---

## “不可证实”（unprovable）是什么意思？

常规情况下，两个参数约束只要相交，审计器都能从交集里挑出一个固定代表值作为
witness（例如 `int × slug` → `1`，`uuid × slug` → 全零 UUID）。

但可达集合是按“完整规范化后的请求路径”定义的。如果两个约束**在理论上相交**，
交集里却取不到任何能通过完整规范化往返校验、或落在交集内的代表值，审计器不会
编造一个假路径，而是如实返回：

```json
{
  "kind": "partial_overlap",
  "status": "unprovable",
  "witness_path": null,
  "reason": "parameter constraints intersect at segment N but no constructible witness exists in their intersection"
}
```

内置的 `int / uuid / slug / 通用` 类型之间不存在这种情况（交集非空则必有 witness）；
该状态主要为通过 `app.constraints.register_constraint` 注册的扩展约束保留
（仓库测试 `tests/test_unprovable.py` 演示了这一路径）。

---

## 项目结构

```
app/
  normalization.py   # URL 百分号解码、RFC 3986 点段移除、尾随斜杠/空段规则
  constraints.py     # int/uuid/slug/通用约束、交集与包含层级、扩展点
  templates.py       # 模板词法/语法解析与规范化
  analyzer.py        # 可达集合相交、覆盖关系、最短 witness、往返校验
  service.py         # 编排：解析分组、成对判定、稳定排序、建议顺序、汇总
  exporter.py        # 路由配置 -> OpenAPI 3.1 paths 片段（参数 schema、operationId）
  render.py          # 按模板与参数值反向生成 canonical path（约束校验、单段编码）
  publish.py         # 活动版本存储：候选校验、CAS 替换、原子落盘与重启恢复
  compare.py         # 只读预检：固定活动快照，比较样本在两侧的路由归属
  migrate.py         # 只读迁移：旧链接经显式 id 映射到目标路由并生成 canonical path
  models.py          # Pydantic 请求/响应模型
  main.py            # FastAPI 路由：/analyze、/export-openapi、/render-paths、
                     # /publish、/active、/precheck、/migrate-links、/healthz
tests/               # pytest 测试（规范化、模板、相交、API、导出、反向渲染、
                     # 发布/活动版本、归属预检、旧链接迁移、unprovable）
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```

## 运行测试

```bash
pip install -r requirements-dev.txt
pytest -q
```

## 典型冲突示例

| 路由 A | 路由 B | 结论 | witness |
| --- | --- | --- | --- |
| `/users/{id:int}` | `/users/1` | 部分相交（静态更具体） | `/users/1` |
| `/users/{id:int}` | `/users/me` | 不相交 | — |
| `/u/{id:int}` | `/u/{id:uuid}` | 不相交（约束互斥） | — |
| `/f/{s:slug}` | `/f/{i:int}` | int 被 slug 完全覆盖 | `/f/1` |
| `/static/**` | `/static/a/b` | 静态子路径被通配完全遮蔽 | `/static/a/b` |
| `/static/**` | `/` | 不相交（前缀不同） | — |
| `/a/{x}/c` | `/a/b/{y}` | 部分相交，互不为超集 | `/a/b/c` |
| `/x/./y` | `/x/y` | 规范化后重复 | `/x/y` |
| `/a%2Fb` | `/a/b` | 不相交（`%2F` 不是分隔符） | — |
| `/%41` | `/A` | 规范化后重复 | `/A` |
