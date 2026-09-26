# 模块接口契约与并行开发分工（v1.1）

> 多个 sub-agent 并行开发时，彼此只通过本文件定义的函数签名和数据文件交互。
> 数据结构以 `src/rfq_agent/models/` 中已实现的 Pydantic 模型为准（与 DESIGN §6.1 一致，个别字段以代码为准，如 `RoutingOp.service_code`）。
> 业务参数一律从 `rfq_agent.config.BIZ` 读取，不在模块里写死。

## 0. 公共规则

1. **只改自己负责的文件**（见 §2 分工表）。需要共享文件（`config.py`、`models/`、`data/db.py`、`data/repositories.py`、`pyproject.toml`、`tests/conftest.py`）的改动：阶段 1 不允许改；如确实需要，在最终汇报中说明，由主控统一改。例外见分工表"可改共享文件"列。
2. 不执行 `git commit`，由主控在每个阶段结束后统一提交。
3. 每个模块配 pytest 测试，测试文件名带模块前缀（如 `tests/test_costing.py`），只跑自己的测试 + 全量测试确认没有破坏别人。
4. 用 `uv run ruff check <自己的文件>` 和 `uv run ruff format <自己的文件>` 保持格式。
5. 代码风格：与现有代码一致（`from __future__ import annotations`、类型标注、简短 docstring、注释少而准）。
6. 所有演示数据都是合成的，不出现任何真实公司、真实人名。

## 1. 模块接口

### 1.1 几何与毛坯 `agents/geometry.py`（planning 负责）

```python
@dataclass
class Stock:
    kind: Literal["bar", "plate", "none"]     # 装配件 = none
    diameter_mm: float | None
    length_mm: float
    width_mm: float | None
    thickness_mm: float | None
    volume_cm3: float
    description: str                          # "Rundstahl Ø45 × 225 mm" 之类，英文即可

def envelope_volume_cm3(spec: DrawingSpec) -> float
def finished_volume_cm3(spec: DrawingSpec) -> float        # DESIGN §5.7
def select_stock(spec: DrawingSpec) -> Stock                # DESIGN §5.6 标准棒料/板厚系列
def weight_kg(volume_cm3: float, density_g_cm3: float) -> float
```

### 1.2 工艺路线 `agents/routing_rules.py`（planning 负责）

```python
def rule_routing(spec: DrawingSpec, material: Material | None, services: dict) -> tuple[list[RoutingOp], list[str]]
    # R01–R14；返回 (工序列表, uncovered_features 的 feature id 列表)
    # 外协工序：outsourced=True, service_code in {"HT_QT","HT_CASE","ANODIZE","ZINC","BLACK"}, setup/cycle = 0
def calibrate(ops: list[RoutingOp], spec: DrawingSpec, similar: list[SimilarPart], part_repo: PartRepo) -> list[RoutingOp]
    # DESIGN §5.7 第 2 步：Top-1 score ≥ BIZ.blend_min_score 时同工位混合；参考件多出的工序 → suggested=True
def plan_routing(spec, material, services, similar, part_repo) -> tuple[list[RoutingOp], list[str]]
    # = rule_routing + calibrate
```

### 1.3 BOM `agents/bom.py`（planning 负责）

```python
def build_bom(spec: DrawingSpec, material: Material | None, routing: list[RoutingOp],
              catalog: CatalogRepo, parts: PartRepo) -> list[BOMLine]
    # 单件：raw_material 行（unit="kg", qty_per=毛坯重量, unit_cost=材料 €/kg）+ 外协 service 行（信息性，unit_cost=None，成本在 costing 按工序算）
    # 装配件：明细表逐行 catalog → internal → new
```

### 1.4 成本 `agents/costing.py`（costing 负责）

```python
def compute_costs(*, routing: list[RoutingOp], bom: list[BOMLine], quantities: list[int],
                  finished_weight_kg: float, requested_delivery: date | None, today: date,
                  rates: RateRepo, services: dict, margin_pct: float = BIZ.margin_pct) -> list[CostBreakdown]
    # 工厂 × 数量档；suggested=True 的工序不计入；DESIGN §5.8 全部公式；每行 CostLine 带 formula 与 source
    # 材料费 = Σ raw_material 行 qty_per × unit_cost × (1+scrap)；外购 = Σ catalog 行；内部件 = Σ internal 行；new 行不计价（单独记 reason）
def recommend_plant(costs: list[CostBreakdown], qty: int) -> str | None
def price_deviation(costs: list[CostBreakdown], recommended_plant: str | None, qty: int,
                    similar: list[SimilarPart], new_env_volume_cm3: float, part_repo: PartRepo) -> float | None
```

### 1.5 置信度 `agents/confidence.py`（costing 负责）

```python
def assess(*, spec: DrawingSpec, item: RFQItem, issues: list[ValidationIssue], similar: list[SimilarPart],
           routing: list[RoutingOp], uncovered: list[str], bom: list[BOMLine],
           price_deviation_pct: float | None) -> ConfidenceReport       # DESIGN §5.9
```

### 1.6 校验 / DFM `agents/review_rules.py`（knowledge 负责）

```python
def review_line(*, line_no: int, item: RFQItem, spec: DrawingSpec | None,
                materials: MaterialRepo, parts: PartRepo) -> list[ValidationIssue]
    # DESIGN §5.3 全部规则；每条 kb_ref 必须在知识库中存在
RULES: list[...]    # 注册表，便于文档与测试遍历
```

### 1.7 知识库 `agents/knowledge.py`（knowledge 负责）

```python
@dataclass
class Chunk: id: str; file: str; heading: str; text: str       # id = "dfm_guidelines.md#deep-holes"
class KnowledgeBase:
    @classmethod
    def load(cls, dir: Path = KNOWLEDGE_DIR) -> KnowledgeBase
    def get(self, chunk_id: str) -> Chunk | None
    def search(self, query: str, k: int = 4) -> list[tuple[Chunk, float]]
```
（带 LLM 的问答 `answer()` 不在今天范围内）

### 1.8 相似件 `agents/similarity.py`（history 负责，阶段 2）

```python
def featurize_spec(spec: DrawingSpec, materials: MaterialRepo) -> dict
def featurize_part(row: dict, materials: MaterialRepo) -> dict
def find_similar(spec: DrawingSpec, parts: PartRepo, materials: MaterialRepo,
                 exclude_part_id: int | None = None, top_k: int = BIZ.similar_top_k) -> list[SimilarPart]
def reuse_hint(spec: DrawingSpec, similar: list[SimilarPart], line_no: int) -> ValidationIssue | None
    # score ≥ 0.92 且材料相同、主尺寸差 < 5% → INFO "REUSE-001"，kb_ref="quoting_policy.md#part-reuse"
```

### 1.9 LLM 层 `llm/`（llm 负责）

```python
class LLMClient:
    def structured(self, *, task: str, prompt_version: str, system: str, user_text: str,
                   images: list[Path] = [], schema: type[T], max_retries: int = 2, rfq_id: str | None = None) -> T
    def text(self, *, task: str, prompt_version: str, system: str, user_text: str, rfq_id: str | None = None) -> str
def get_client() -> LLMClient      # 按环境变量构造；LLM_MODE = live | record | replay | auto
```
- Provider：`ollama`（**默认**，本机 `http://localhost:11434`；默认模型 `gemma4:12b`，文本与视觉共用；可分别配置 `LLM_MODEL` / `LLM_VISION_MODEL`）、`openai_compat`、`anthropic`。
- 本机内存约束：同一时间只加载一个本地模型（`OLLAMA_MAX_LOADED_MODELS=1`），避免与开发中的其他进程抢内存。
- `auto`：缓存命中 → 用缓存；未命中且模型可用 → live 并写缓存；都不行 → 明确报错。
- 失败留证据到 `FAILURE_DIR`。

### 1.10 抽取 `agents/intake.py`、`agents/drawing.py`、`agents/postprocess.py`（llm 负责）

```python
def parse_rfq(rfq_dir: Path, client: LLMClient) -> RFQRequest            # 读 email.txt + drawings/ 附件名
def render_pdf(pdf: Path, dpi: int = 200) -> list[Path]
def crop_regions(page_png: Path, client: LLMClient) -> dict[str, Path]   # title_block / parts_list / notes / view_* ，DESIGN §15-6
def extract_drawing(pdf: Path, client: LLMClient, materials: MaterialRepo) -> tuple[DrawingSpec, list[Path]]
def postprocess(spec: DrawingSpec, materials: MaterialRepo) -> DrawingSpec # 单位、材料别名、IT 等级、包络、一致性
def it_grade(nominal_mm: float, upper: float | None, lower: float | None, fit: str | None) -> int | None
```

## 2. 阶段与分工

| 阶段 | sub-agent | 负责文件 | 可改共享文件 | 依赖 |
|---|---|---|---|---|
| 1 | **samples** | `scripts/gen_drawings.py`、`data/samples/**`（5 张 PDF + PNG 预览、4 封 email.txt、5 份 expected JSON）、`tests/test_samples.py` | — | 无 |
| 1 | **knowledge** | `data/knowledge/*.md`、`agents/knowledge.py`、`agents/review_rules.py`、`tests/test_knowledge.py`、`tests/test_review_rules.py` | — | 无 |
| 1 | **llm** | `llm/**`、`agents/intake.py`、`agents/drawing.py`、`agents/postprocess.py`、`tests/test_llm*.py`、`tests/test_postprocess.py`、`.env.example` | `config.py`（仅 `LLMSettings`） | 无 |
| 1 | **planning** | `agents/geometry.py`、`agents/routing_rules.py`、`agents/bom.py`、`tests/test_routing.py`、`tests/test_bom.py` | — | 无 |
| 1 | **costing** | `agents/costing.py`、`agents/confidence.py`、`tests/test_costing.py`、`tests/test_confidence.py` | — | 无 |
| 2 | **history** | `scripts/gen_history.py`、`agents/similarity.py`、`data/history/**`、`tests/test_similarity.py`、`tests/test_history.py` | `data/db.py`、`data/repositories.py`、`scripts/build_db.py` | samples, costing, planning |
| 2 | **extraction** | `fixtures/llm_cache/**`、prompt 迭代（`llm/prompts/`）、`eval/eval_extraction.py`、`eval/reports/extraction.md` | `llm/**` | samples, llm |
| 3 | **graph** | `graph/**`、`agents/quote_writer.py`、`templates/**`、`cli.py`、`tests/test_graph.py` | `models/`（仅新增字段） | 全部 |
| 4 | **ui** | `ui/**` | — | graph |
| 4 | **eval** | `eval/backtest_costing.py`、`eval/eval_routing.py`、`eval/feedback_report.py`、`eval/reports/**` | 规则系数/阈值（需在报告中写明改了什么、为什么） | graph |
