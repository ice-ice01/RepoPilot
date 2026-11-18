# 前端开发者的 Python 实战入门：结合 DevFlow-AI 学习

适用对象：会 JavaScript / TypeScript，第一次系统学习 Python。

目标不是背完语法，而是：能写数据处理脚本，能读懂本项目的 Issue 接口，能独立写一个带参数校验的小 API。

本文基于当前项目源码编写。项目目录叫 DevFlow-AI，README 产品名为“研伴 RepoPilot”；前端使用 Next.js / React / TypeScript，后端使用 Python / FastAPI / Pydantic / SQLAlchemy。本文源码定位对应编写时版本，后续修改可能导致行号变化。

建议分三轮阅读：第一轮读 1～7 节并运行脚本；第二轮读 8～12 节并启动教学 API；第三轮读 13～17 节，跟踪真实接口并做练习。

## 1. 先运行，再学习

本机项目已有 `backend/.venv`，读取到的解释器版本为 Python 3.12.3。教程示例按 Python 3.10+ 语法编写；安装完整业务依赖时还需遵守各依赖的版本要求。以下命令均在 PowerShell 中执行。

```powershell
cd D:\Desktop\DevFlow-AI
.\backend\.venv\Scripts\python.exe --version
.\backend\.venv\Scripts\python.exe -X utf8 .\docs\learning\python_basics.py
```

练习脚本只用标准库，不联网、不读写业务数据库、不调用模型，也不会修改文件。`-X utf8` 为本次 Python 进程启用 UTF-8 模式，减少 Windows 终端中文编码不一致的问题。你会看到 Issue 分类、统计、类方法、异步与 JSON 的演示。统计结果应包含：

```json
{
  "total": 3,
  "open": 2,
  "closed": 1,
  "categories": {"Bug": 1, "Feature": 1}
}
```

运行配套测试：

```powershell
.\backend\.venv\Scripts\python.exe -m unittest discover -s .\docs\learning -p test_python_basics.py -v
```

如果在另一台机器从零开始，可在项目根目录创建独立学习环境，而不要覆盖已有业务环境：

```powershell
py -3 -m venv .venv-learning
.\.venv-learning\Scripts\python.exe .\docs\learning\python_basics.py
```

`venv` 是隔离 Python 解释器及安装包的环境，不只是 `node_modules` 的同义词。后面涉及第三方库时，用哪个解释器运行，就用哪个解释器的 `-m pip` 安装。直接调用环境内的解释器不需要激活，也不会遇到 PowerShell 激活脚本执行策略的问题。

## 2. JS / TS 到 Python 的速查表

| JS / TS 概念 | Python 写法 | 注意点 |
| --- | --- | --- |
| `let name = "Tom"` | `name = "Tom"` | 无需声明关键字，也没有同等的 `const` 绑定 |
| `true / false / null` | `True / False / None` | 大小写固定 |
| 数组 | `list` | `.append()` 追加元素 |
| 普通对象作数据容器 | `dict` | 通常用 `item["title"]`，不支持字典点访问 |
| `Set` | `set` | 空集合写 `set()`；`{}` 是空字典 |
| 模板字符串 | `f"你好，{name}"` | 字符串前加 `f` |
| `&& / || / !` | `and / or / not` | `or` 不是空值合并运算符 |
| `arr.includes(x)` | `x in arr` | `in` 用于字典时查键 |
| `for...of` | `for item in items:` | 不写大括号，靠缩进分块 |
| `function f()` | `def f():` | 函数返回值缺省为 `None` |
| 三元表达式 | `a if condition else b` | 顺序与 JS 不同 |
| 展开 / 剩余参数 | `*args / **kwargs` | `*` 处理位置参数，`**` 处理关键字参数 |
| `throw / catch` | `raise / except` | `try / finally` 仍有 |
| `Promise.all(...)` | `await asyncio.gather(...)` | Python 协程对象不会因创建而自动执行 |

不要逐个符号硬翻译。Python 的真值、作用域、异步启动方式都与 JS 有细节差异，下面会逐一解释。

## 3. 变量、类型、缩进与作用域

```python
title = "Login API returns 500"  # str
number = 101                      # int
confidence = 0.85                 # float
is_open = True                    # bool
body = None                       # NoneType

if is_open:
    print(f"#{number} {title}")
else:
    print("已关闭")
```

Python 用缩进表示代码块，通常是 4 个空格；冒号后开始一个块。不强制分号。注释以 `#` 开头。

Python 也是动态类型，但不等于“会随便隐式转换”：

```python
print("Issue #" + str(number))  # 必须显式转字符串
print(1 == "1")                # False
# print(1 + "1")              # TypeError
```

与 JS `let` 不同，`if` / `for` 不创建独立块级作用域。函数会创建局部作用域，不要依赖循环变量在循环后是否存在：空循环可能根本没有给它赋值。

`is` 判断“是否同一个对象”，`==` 判断“值是否相等”。空值判断习惯写 `value is None`，不要把 `is` 当作 JS 的 `===`。

### 类型注解不是运行时校验

```python
def format_title(title: str) -> str:
    return f"Issue: {title}"

print(format_title(123))  # 仍能运行，输出 Issue: 123
```

`: str` 与 `-> str` 服务于文档、IDE 和静态检查。Python 不会仅凭注解自动拒绝错误类型。第 11 节的 Pydantic 才承担运行时解析和校验。

## 4. 四种常见容器：list、dict、tuple、set

### 4.1 列表：对应大多数数组场景

```python
labels = ["bug", "backend"]
labels.append("urgent")
print(labels[0])       # bug
print(labels[-1])      # urgent，最后一个
print(labels[:2])      # ['bug', 'backend']，不含右端点
print(len(labels))     # 3
```

`.append()` 返回 `None`，不要写 `labels = labels.append("bug")`。`.sort()` 也原地修改且返回 `None`；需要新列表时用 `sorted(labels)`。

列表越界访问抛出 `IndexError`，不是返回 `undefined`。切片通常会生成新列表。

### 4.2 字典：数据对象，但不是类实例

```python
issue = {"title": "登录失败", "body": None, "labels": ["bug"]}
print(issue["title"])                  # 已知必填字段
print(issue.get("author", "匿名"))    # 缺键时使用默认值
print(issue.get("body", "无正文"))    # None：键存在，默认值不生效
print(issue.get("body") or "无正文") # 无正文：所有假值都会被替代

for key, value in issue.items():
    print(key, value)
```

`issue["missing"]` 会抛出 `KeyError`；`.get()` 适合可选字段。`issue.title` 不适用于普通字典；项目中 `issue.title` 常见于 ORM 或 Pydantic 模型实例。

字典键可以是字符串等可哈希对象，并不限于字符串。JSON 对象的键则是字符串。

### 4.3 元组与集合

```python
pair = (101, "登录失败")
number, title = pair             # 解包
single = (101,)                   # 单元素元组需要逗号

unique_labels = {"bug", "bug", "frontend"}
print(len(unique_labels))        # 2
print("bug" in unique_labels)   # True
```

元组不可直接增删改元素，但其内部引用的可变对象仍能改变。集合适合去重和成员检查，不要依赖它的遍历顺序。

### 4.4 引用与浅拷贝

```python
original = {"labels": ["bug"]}
copied = {**original}
copied["labels"].append("urgent")
print(original["labels"])  # ['bug', 'urgent']
```

和 JS 的对象展开类似，这只是浅拷贝。`dict.copy()` 也一样。确实需要独立的嵌套对象时可考虑 `copy.deepcopy()`，但不要为所有数据无脑深拷贝。

## 5. 条件、循环与数据转换

### 5.1 Python 中空列表和空字典是假值

```python
if []:
    print("不会执行")

print(bool({}))  # False；与 JS 的空对象不同
print(bool(""))  # False
print(bool(0))   # False
```

本项目常用 `body = input_data.get("body") or ""`。它会替换 `None`、空字符串、0、False 等所有假值。

只想替换 `None` 时，应明确写：

```python
value = 0
result = value if value is not None else 10
print(result)  # 0，不会把有效的 0 替换掉
```

### 5.2 从 JS 的 filter / map 迁移到推导式

```javascript
const titles = issues
  .filter(issue => issue.state === "open")
  .map(issue => issue.title);
```

```python
issues = [
    {"title": "登录失败", "state": "open"},
    {"title": "更新文档", "state": "closed"},
]
titles = [issue["title"] for issue in issues if issue["state"] == "open"]
print(titles)  # ['登录失败']
```

阅读顺序是：遍历 `issues` → 根据 `if` 筛选 → 计算左侧表达式。复杂到难读时就写普通 `for` 循环。

```python
for index, issue in enumerate(issues, start=1):
    print(index, issue["title"])

numbers = [1, 2, 3]
print(sum(numbers))
print(any(number > 2 for number in numbers))  # 类似 some，True
print(all(number > 0 for number in numbers))  # 类似 every，True
print(list(range(3)))                        # [0, 1, 2]
```

`any(...)` 中的 `(... for ...)` 是生成器表达式，按需产出元素，可以短路，不必先构造完整列表。空集合的 `any` 是 False，`all` 是 True。

源码对应：[issue_agent.py · line 48](D:/Desktop/DevFlow-AI/backend/app/services/agents/issue_agent.py:48) 使用列表推导式归一化标签；[issue_agent.py · line 123](D:/Desktop/DevFlow-AI/backend/app/services/agents/issue_agent.py:123) 用 `any` 判断分类关键词。

## 6. 函数：参数、返回值与解包

```python
def format_issue(title: str, number: int = 0) -> str:
    return f"#{number} {title}"

print(format_issue("登录失败", 101))
print(format_issue(title="登录失败", number=101))
```

Python 支持按参数名调用，这让配置型调用很直观；默认参数通常放在必填参数后。

```python
def build_request(path: str, *, timeout: int = 30) -> dict:
    return {"path": path, "timeout": timeout}

build_request("/issues", timeout=10)
# build_request("/issues", 10)  # TypeError：* 后只能传关键字参数
```

`*args` 收集额外位置参数为元组，`**kwargs` 收集额外关键字参数为字典；调用时也能反向展开：

```python
def collect(*args, **kwargs):
    return args, kwargs

print(collect(1, 2, state="open"))
data = {"title": "登录失败", "number": 101}
print(format_issue(**data))

defaults = {"priority": "P2", "category": "Feature"}
actual = {"priority": "P1"}
merged = {**defaults, **actual}  # 后面的同名键覆盖前面
```

源码对应：[issue_agent.py · line 79](D:/Desktop/DevFlow-AI/backend/app/services/agents/issue_agent.py:79) 用上述合并方式将模型输出覆盖到兜底结果上；后续还会限制负责人等字段并做校验，不能把任意模型输出直接当可信结果。

### 重要陷阱：普通函数的默认列表会跨调用共享

```python
# 不推荐：默认列表在函数定义时创建一次
def add_bad(label, labels=[]):
    labels.append(label)
    return labels

def add_label(label: str, labels: list[str] | None = None) -> list[str]:
    if labels is None:
        labels = []
    labels.append(label)
    return labels
```

如果调用者传入列表，后一函数仍会修改传入的列表。若需要无副作用，先 `labels = list(labels) if labels is not None else []`。

## 7. 模块、包和入口

一个 `.py` 文件是一个模块。包负责组织模块，常规包通常带 `__init__.py`；现代 Python 也支持没有该文件的命名空间包。

```python
import json
from pathlib import Path
from app.services.agents.issue_agent import IssueAgent
```

最后一种导入要求 Python 能找到 `app`。本项目的 `app` 位于 `backend` 下，因此读取业务模块时，通常在 `backend` 目录运行：

```powershell
cd D:\Desktop\DevFlow-AI\backend
.\.venv\Scripts\python.exe -c "from app.services.agents.base import BaseAgent; print(BaseAgent.__name__)"
```

导入模块时，其顶层代码可能执行；不要随意导入 `app.main` 来练习，业务应用存在配置、连接和启动生命周期等额外行为。

```python
def main():
    print("直接运行时执行")

if __name__ == "__main__":
    main()
```

入口保护意味着：直接运行文件时执行 `main()`，被其他模块导入时不执行这个调用。它不会阻止入口保护之外的顶层代码执行。

`python -m unittest` 中的 `-m` 表示“把模块作为程序运行”；`python -m pip` 能确保 pip 对应当前解释器。依赖清单在 [requirements.txt](D:/Desktop/DevFlow-AI/backend/requirements.txt)，不需要为学语法就安装全部 AI 依赖。

## 8. 异常、文件、路径与 JSON

### 8.1 明确捕获可预期错误

```python
raw = "not-a-number"
try:
    number = int(raw)
except ValueError as error:
    print(f"编号格式错误：{error}")
else:
    print(f"转换成功：{number}")
finally:
    print("无论成功失败都会执行")
```

主动报错使用 `raise ValueError("说明")`。不要习惯性 `except Exception: pass`，否则真正的 bug 会消失。项目 Agent 中存在宽泛捕获的兜底策略，这是特定业务选择，不是推荐的通用入门模板。

### 8.2 JSON 对象不是 Python 对象本身

```python
import json

data = {"title": "登录失败", "enabled": True, "body": None}
text = json.dumps(data, ensure_ascii=False)  # Python → JSON 字符串
restored = json.loads(text)                 # JSON 字符串 → Python
print(restored["enabled"])                 # True
```

记忆：`dumps / loads` 面向字符串；`dump / load` 面向文件对象。JSON 使用 `true / null`，Python 使用 `True / None`。

### 8.3 文件操作：显式编码、路径和覆盖行为

下面是可选练习，执行时会在当前目录写入 `learning-output/issues.json`；重复运行会覆盖该练习文件，请先确认里面没有需要保留的数据。配套主脚本不会执行这个写文件示例。

```python
import json
from pathlib import Path

output_dir = Path("learning-output")
output_dir.mkdir(exist_ok=True)
output = output_dir / "issues.json"

with output.open("w", encoding="utf-8") as file:
    json.dump([{"title": "登录失败"}], file, ensure_ascii=False, indent=2)

with output.open(encoding="utf-8") as file:
    issues = json.load(file)
print(issues)
```

`with` 离开代码块时会关闭文件，即使发生异常。相对路径基于当前工作目录，而不是自动基于脚本目录；需要脚本位置时用 `Path(__file__).resolve().parent`。

## 9. 类、self、继承和 dataclass

```python
class Issue:
    def __init__(self, title: str):
        self.title = title

    def summary(self) -> str:
        return f"Issue: {self.title}"

issue = Issue("登录失败")  # 不写 new
print(issue.title)
print(issue.summary())
```

`self` 类似实例方法中的 `this`，但需要显式写在方法参数里；调用时 Python 自动传入实例。`__init__` 初始化实例；类属性与实例属性要分清，共享可变类属性也容易造成状态串用。

`_helper` 的单下划线表示“内部使用”的约定，不是强制私有。`__init__`、`__name__` 这类双下划线名字通常是 Python 约定的特殊协议。

项目中的继承关系见 [base.py · line 5](D:/Desktop/DevFlow-AI/backend/app/services/agents/base.py:5) 与 [issue_agent.py · line 10](D:/Desktop/DevFlow-AI/backend/app/services/agents/issue_agent.py:10)：

```python
class IssueAgent(BaseAgent):
    ...
```

`BaseAgent` 是抽象基类，规定子类实现 `run`。真实的 `IssueAgent` 默认创建 LLM 客户端，也允许注入假的客户端做离线测试；这叫依赖注入，概念并不依赖框架。

只想保存结构化数据，可用标准库的 `dataclass`：

```python
from dataclasses import dataclass, field

@dataclass
class Issue:
    title: str
    labels: list[str] = field(default_factory=list)

issue = Issue(title="登录失败")
```

它自动生成常用初始化和显示方法；`default_factory=list` 为每个实例生成独立列表。普通 dataclass 默认不提供 Pydantic 那样的运行时字段校验。

## 10. async / await：最值得注意的 JS 差异

JavaScript 中调用 `async` 函数会启动函数执行，并返回 Promise。Python 中调用 `async def` 得到的是协程对象，其函数体不会因此自动运行；需要 `await` 或调度为 Task。

```python
import asyncio

async def fetch_title(number: int) -> str:
    await asyncio.sleep(0.01)  # 模拟等待 I/O
    return f"Issue #{number}"

async def main():
    one = await fetch_title(101)  # 等待一个协程
    many = await asyncio.gather(fetch_title(102), fetch_title(103))
    print(one)
    print(many)

if __name__ == "__main__":
    asyncio.run(main())
```

`gather` 可以并发等待多个 I/O 任务，结果顺序与传入顺序一致。`await a(); await b()` 通常仍是顺序执行。实际批量请求还要限制并发，避免触发服务限流。

异步不等于 CPU 并行。不要在 `async def` 中用 `time.sleep()` 假装等待，也不要以为同步数据库查询会因为套上 `async` 自动变成非阻塞。独立脚本用 `asyncio.run()` 启动；FastAPI 已有事件循环，在其异步函数中直接 `await`，不要嵌套调用 `asyncio.run()`。

项目的 HTTP 封装见 [client.py · line 55](D:/Desktop/DevFlow-AI/backend/app/services/github/client.py:55)：

```python
response = await shared_http_client().get(
    f"{self.base_url}{path}", headers=self._headers(), params=params, timeout=30
)
response.raise_for_status()
return response.json()
```

这里 `httpx` 的 `response.json()` 是同步解析，不需要 `await`，与浏览器 `fetch` 的 `await response.json()` 不同。`raise_for_status()` 会把 HTTP 错误转成异常。

可选的只读本地请求，前提是你已经自行启动正式后端：

```python
import asyncio
import httpx

async def main():
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get("http://127.0.0.1:8000/health")
        response.raise_for_status()
        print(response.json())

if __name__ == "__main__":
    asyncio.run(main())
```

正式业务接口存在登录 Cookie、CSRF 和权限检查。不要拿未登录的 Python 请求直接调用 `/api/issues/...` 再把 401 / 403 误判为语法错误；入门优先使用下一节的独立教学 API。

## 11. Pydantic：把 TS 类型变成运行时数据边界

TypeScript 类型在运行时消失。Pydantic 不只是类型提示，它能在收到字典等数据时解析并校验。

```python
from typing import Literal
from pydantic import BaseModel, Field, ValidationError

class Analysis(BaseModel):
    category: Literal["Bug", "Feature"]
    confidence: float = Field(ge=0, le=1)
    labels: list[str] = Field(default_factory=list)

valid = Analysis.model_validate({"category": "Bug", "confidence": 0.8})
print(valid.category)       # Bug，模型用点访问
print(valid.model_dump())   # 普通 Python 字典
print(valid.model_dump_json())  # JSON 字符串

try:
    Analysis.model_validate({"category": "Unknown", "confidence": 2})
except ValidationError as error:
    print(error.errors())
```

`Literal` 限定可选值，`Field(ge=0, le=1)` 限定数值范围。Pydantic 默认会对部分输入做类型转换，如数字字符串转数字；需要禁止转换时再研究严格模式。`model_dump()` 不保证所有值都已转成 JSON 原生类型，例如 UUID / datetime；需要 JSON 兼容字典时可用 `model_dump(mode="json")`。

源码定义见 [analysis.py · line 28](D:/Desktop/DevFlow-AI/backend/app/schemas/analysis.py:28)，调用校验的位置见 [issue_agent.py · line 37](D:/Desktop/DevFlow-AI/backend/app/services/agents/issue_agent.py:37)。读这两个位置就能把“数据契约”和“实际执行校验”对应起来。

注意：本项目部分 Pydantic 模型直接写 `labels: list[str] = []`。Pydantic v2 对这种不可哈希默认值会为实例深拷贝，不能照搬普通函数默认列表的结论；学习时统一用 `Field(default_factory=list)` 更直观。

## 12. FastAPI：独立运行你的第一个后端接口

配套 [mini_api.py](D:/Desktop/DevFlow-AI/docs/learning/mini_api.py) 是独立教学服务，不注册到正式应用，不连接数据库，不读取生产配置；只处理内存数据。仅供本机学习，不是生产安全模板。

在项目根目录运行，使用 8010 端口避免与正式后端 8000 冲突：

```powershell
cd D:\Desktop\DevFlow-AI
.\backend\.venv\Scripts\python.exe -m uvicorn mini_api:app --app-dir docs/learning --host 127.0.0.1 --port 8010
```

打开 [教学接口文档](http://127.0.0.1:8010/docs)，展开 `POST /analyze`，点 Try it out，输入：

```json
{"title": "Login API returns 500", "labels": ["bug"]}
```

预期返回：

```json
{"category": "Bug", "title": "Login API returns 500"}
```

再把 `title` 改为 `""`，应返回 422 校验错误。PowerShell 也可以请求：

```powershell
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8010/analyze' -ContentType 'application/json' -Body '{"title":"Login API returns 500","labels":["bug"]}'
```

`Ctrl+C` 停止教学服务。如果使用独立 `.venv-learning`，先自行安装教学依赖，再将启动命令中的解释器路径替换成该环境：

```powershell
.\.venv-learning\Scripts\python.exe -m pip install fastapi==0.115.6 uvicorn==0.34.0 pydantic==2.10.4
```

关键结构：

```python
@app.post("/analyze", response_model=AnalyzeResponse)
def analyze(payload: AnalyzeRequest) -> AnalyzeResponse:
    return AnalyzeResponse(
        category=classify_issue(payload.title, payload.labels),
        title=payload.title,
    )
```

逐句读：`@app.post` 注册路由；`payload: AnalyzeRequest` 让 FastAPI 从 JSON 请求体解析模型；函数处理业务；`response_model` 定义响应契约和过滤规则。

装饰器 `@decorator` 可以先理解为“函数定义完成时把函数交给装饰器处理”。这里用于注册路由，并非前端点击后才运行的特殊语法。

参数来自哪里由声明方式决定：路径中的 `{issue_id}` 对应路径参数；普通标量通常对应查询参数；Pydantic 模型通常对应 JSON 请求体；`Depends(...)` 对应依赖注入。不要以为所有函数参数都来自请求体。

## 13. 一条真实请求如何穿过当前项目

先看最小调用链，不要一开始进入很长的聊天 Agent：

```text
Issue 页面点击“分析当前 Issue”
  → apiPost：JSON 请求 + Cookie / CSRF 处理
  → POST /api/issues/{issue_id}/analyze
  → 查询 Issue，组装上下文
  → IssueAgent.run：规则兜底 + 模型结构化输出 + 校验
  → 保存 AnalysisResult
  → 返回 IssueAnalysisResponse 给前端
```

按下面的顺序点击源码读：

1. [page.tsx · line 33](D:/Desktop/DevFlow-AI/frontend/app/issues/page.tsx:33)：前端触发分析，传入 `{}`。
2. [api.ts · line 67](D:/Desktop/DevFlow-AI/frontend/lib/api.ts:67)：`apiPost` 序列化请求体；[api.ts · line 42](D:/Desktop/DevFlow-AI/frontend/lib/api.ts:42) 实际携带 Cookie 并处理 CSRF。
3. [main.py · line 194](D:/Desktop/DevFlow-AI/backend/app/main.py:194)：公共 API 路由带权限依赖；[main.py · line 198](D:/Desktop/DevFlow-AI/backend/app/main.py:198) 将 Issue 路由挂到 `/api`。
4. [issues.py · line 283](D:/Desktop/DevFlow-AI/backend/app/api/routes/issues.py:283)：进入 `analyze_issue`；本路由未声明 JSON 请求体模型，所以前端传入的 `{}` 不是分析数据来源。
5. [issues.py · line 98](D:/Desktop/DevFlow-AI/backend/app/api/routes/issues.py:98)：把数据库 Issue 实例整理为字典；[issues.py · line 111](D:/Desktop/DevFlow-AI/backend/app/api/routes/issues.py:111) 组装证据与上下文。
6. [issue_agent.py · line 27](D:/Desktop/DevFlow-AI/backend/app/services/agents/issue_agent.py:27)：执行分析；[issue_agent.py · line 43](D:/Desktop/DevFlow-AI/backend/app/services/agents/issue_agent.py:43) 是可读的规则兜底路径。
7. [issues.py · line 169](D:/Desktop/DevFlow-AI/backend/app/api/routes/issues.py:169)：保存分析记录，`add → commit → refresh`；[issues.py · line 296](D:/Desktop/DevFlow-AI/backend/app/api/routes/issues.py:296) 构造响应。
8. [issues.py · line 25](D:/Desktop/DevFlow-AI/backend/app/schemas/issues.py:25)：响应模型包含 `analysis_id` 和结构化 `result`。

这里真实的 URL 由 `/api` 挂载前缀和路由路径拼接，不要只看装饰器里的 `/issues/...` 就漏掉前缀。`issue_id` 是项目数据库的 UUID，不是 GitHub 上展示的 `#101`。

`IssueAgent` 继承自 `BaseAgent`，但这个领域分析器不是自主工具循环：其 `run` 先生成规则结果，再请求一次结构化模型输出并校验。`available_tools` 是能力声明，不能仅凭这个列表就认为本类自己会循环调用所有工具。

## 14. 数据库模型、Schema、Service：不要混成一个概念

| 层 | 本项目示例 | 负责什么 |
| --- | --- | --- |
| ORM 数据模型 | `db/models.py` 中的 `Issue` | 数据库表、字段、关系和持久化映射 |
| API / 输出模型 | `schemas/issues.py`、`schemas/analysis.py` | 输入输出结构、解析和校验 |
| Service / Agent | `services/agents/issue_agent.py` | 业务分析和规则 |
| 路由 | `api/routes/issues.py` | 接收 HTTP、调用业务、组织响应 |

SQLAlchemy ORM 模型见 [models.py · line 124](D:/Desktop/DevFlow-AI/backend/app/db/models.py:124)；它与 [issues.py · line 9](D:/Desktop/DevFlow-AI/backend/app/schemas/issues.py:9) 的 `IssueResponse` 不是同一个类。后者通过 `from_attributes=True` 支持从对象属性中读取字段。

先读查询，不必上来就写数据库：

```python
return (
    db.query(Issue)
    .filter(Issue.repo_id == repo_id)
    .order_by(Issue.number.desc())
    .all()
)
```

意思是：查询 Issue → 限定仓库 → 按编号降序 → 取所有结果。这里 `Issue.repo_id == repo_id` 是 SQLAlchemy 构造 SQL 条件，不是把所有行先加载到 Python 再比较。真实调用见 [issues.py · line 270](D:/Desktop/DevFlow-AI/backend/app/api/routes/issues.py:270)。

本项目此处使用同步 SQLAlchemy Session 与 `db.query` 风格；你可能在新教程里看到 `select(...)` 风格，不要因写法不同就以为是两门语言。

### Depends + yield：把资源生命周期交给框架

[session.py · line 17](D:/Desktop/DevFlow-AI/backend/app/db/session.py:17) 的核心代码：

```python
def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
```

路由参数 `db: Session = Depends(get_db)` 让 FastAPI 执行该依赖并把 Session 注入。`yield` 先把资源交给框架，框架按其生命周期规则恢复生成器，执行清理。不要把它简单理解成普通 `return`，也不要仅凭这段代码断言所有流式场景的精确关闭时刻。

`get_db()` 只展示提供与关闭会话，不会自动替业务函数提交事务。业务写入还需合理处理提交、回滚与异常。`db.commit()` 是真实写入边界，学语法时不需要运行正式写接口。

## 15. 测试：先用确定的数据证明代码正确

配套测试用标准库 `unittest`，不加载后端的测试配置，不需要数据库。读 [test_python_basics.py](D:/Desktop/DevFlow-AI/docs/learning/test_python_basics.py)，先理解“准备输入 → 调用 → 比较结果”。

```python
def test_bug_classification(self):
    self.assertEqual(classify_issue("Login returns 500"), "Bug")
```

项目业务测试使用 pytest，最接近 JS 的 Vitest / Jest。见 [test_issue_agent.py · line 6](D:/Desktop/DevFlow-AI/backend/app/tests/test_issue_agent.py:6) 和 [test_issue_agent.py · line 16](D:/Desktop/DevFlow-AI/backend/app/tests/test_issue_agent.py:16)：

```python
class NoopLLM:
    async def chat_json(self, system_prompt, user_payload):
        return {}

@pytest.mark.asyncio
async def test_issue_agent_fallback_classifies_bug() -> None:
    result = await IssueAgent(llm=NoopLLM()).run(
        {"title": "API returns 500", "body": "Login fails", "labels": ["bug"]},
        {},
    )
    assert result["category"] == "Bug"
```

假的 `NoopLLM` 返回空字典，让测试走规则兜底，不产生模型调用费用。Python 常用“只要对象有需要的方法就能合作”的鸭子类型，这里即使假对象没有继承 `LLMClient`，运行时也能工作；严格静态检查是否接受，取决于注解和协议设计。

等业务依赖准备好后，可以从 `backend` 目录运行项目自己的这组测试，而不是直接跑全部集成测试：

```powershell
cd D:\Desktop\DevFlow-AI\backend
.\.venv\Scripts\python.exe -m pytest app/tests/test_issue_agent.py -q
```

项目测试会加载自身 `conftest.py` 和配置模块，环境要求比纯标准库练习高。教学测试通过，不代表整个生产系统或外部服务已经验证通过。

## 16. 五个动手练习（附思路与自检）

先在练习副本里完成，暂时不要改正式路由或数据库模型。

### 练习 A：提取未关闭的标题

输入 `SAMPLE_ISSUES`，只返回 open Issue 的标题。

参考：

```python
def open_titles(issues: list[dict]) -> list[str]:
    return [issue["title"] for issue in issues if issue.get("state") == "open"]
```

自检：结果长度为 2，空输入返回 `[]`，原始列表没有被修改。

### 练习 B：统计标签出现次数

目标：`bug`、`backend`、`feature`、`frontend`、`docs` 在样例中各出现 1 次。

```python
from collections import Counter

def count_labels(issues: list[dict]) -> dict[str, int]:
    return dict(Counter(
        label
        for issue in issues
        for label in (issue.get("labels") or [])
    ))
```

这是嵌套遍历的生成器表达式。你也可以先用双层 `for` 实现，理解后再简写。这里统计出现次数；如果要求“一条 Issue 中重复标签只算一次”，需要在每条记录内部先去重。

### 练习 C：按相似度筛选并排序

```python
def top_candidates(items: list[dict], threshold: float = 0.8) -> list[dict]:
    return sorted(
        [item for item in items if item["score"] >= threshold],
        key=lambda item: item["score"],
        reverse=True,
    )[:3]
```

`lambda` 是单表达式匿名函数；`key` 提供排序依据，不像 JS `sort((a, b) => ...)` 接收二元比较器。自检：分数 `[0.7, 0.91, 0.8]` 得到 `[0.91, 0.8]`，原始顺序不变。此练习假设输入字段已校验。

### 练习 D：扩展教学分类器并补测试

让 `classify_issue` 支持 `Test` 分类，关键词是 `pytest / coverage / 单测`。至少补 3 个测试：英文关键词、中文关键词、无匹配仍返回 Feature。

同时修改 `mini_api.py` 的 `AnalyzeResponse.category`，增加 `"Test"`。如果只改分类函数而不改响应模型，新分类会在响应构建时校验失败。这正是业务逻辑与数据契约需要同步的原因。也想测试教学 API 时再添加对应请求用例。

### 练习 E：只读跟踪正式代码

打开第 13 节链接，回答：

1. 分析接口的 `issue_id` 来自 URL、查询参数还是请求体？
2. `conversation_id` 缺省值是什么？
3. Issue 不存在时发生什么？
4. 返回给前端前，在哪里检查模型结果结构？
5. 哪一行提交分析记录到数据库？

答案：路径；`None`；抛出 404 `HTTPException`；`IssueAgent.run` 的 `IssueAnalysisOutput.model_validate`，以及兜底构建模型和响应模型等边界；保存函数中的 `db.commit()`。

## 17. 七次学习安排与验收标准

每次 45～90 分钟，根据练习耗时调整，不追求一天记住所有语法。

| 次数 | 阅读与动手 | 完成标志 |
| --- | --- | --- |
| 1 | 第 1～4 节，运行脚本，修改样例数据 | 会读写字典、列表，区分 None 和缺键 |
| 2 | 第 5～6 节，练习 A / B / C | 能独立筛选、统计、排序 |
| 3 | 第 7～8 节，入口、异常、JSON | 能解释导入路径，并读懂文件处理代码 |
| 4 | 第 9～10 节，类与协程 | 能解释 self、构造器、await 和 gather |
| 5 | 第 11～12 节，启动教学 API | 成功请求一次，并能解释一次 422 |
| 6 | 第 13～15 节，追踪真实接口 | 能说清前端、路由、业务、校验与数据库的关系 |
| 7 | 练习 D / E，补测试 | 独立扩展一种分类，并保持契约和测试同步 |

遇到陌生语法的阅读顺序：先看函数输入输出，再看分支与数据流，最后查装饰器、泛型等细节。第一阶段不要求掌握元类、复杂魔术方法、向量库、RAG、LangChain 或多 Agent 编排。先能解释一条普通 Issue 请求，后续看 AI 逻辑才会有清晰的落点。

## 常见问题速查

- `ModuleNotFoundError: app`：优先检查工作目录和解释器；业务命令通常从 `backend` 运行，不要随手到处改 `sys.path`。
- `No module named fastapi`：当前解释器未安装依赖，检查是否误用了系统 Python；无需为标准库练习安装 FastAPI。
- `coroutine was never awaited`：创建了协程但没有 await 或调度。
- `'dict' object has no attribute 'title'`：字典用 `data["title"]`；模型实例才通常用点访问。
- API 返回 422：先查看响应的 `detail`，核对字段类型、必填字段、数值范围；不是一概代表服务器崩了。
- 正式 API 返回 401 / 403：核对登录和权限，不要绕过安全校验来完成学习。
- 输出成了 `None`：检查是否把 `.append()` / `.sort()` 的返回值又赋给了变量。
- 中文文件报解码错误：显式用 `encoding="utf-8"`；确认源文件本身的实际编码。

最小毕业标准：不用复制答案，自己写一个函数，读取一组 Issue 字典，筛选 open，统计分类，输出 JSON；再把这个函数挂到独立 FastAPI 接口，并为正常、空输入、非法输入各补一个测试。
