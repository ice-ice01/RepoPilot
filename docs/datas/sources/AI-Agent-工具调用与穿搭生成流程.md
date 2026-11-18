# Wardrobe Agent：Tool Calling 与穿搭生成流程

## 一、先记住核心结论

模型不直接连接数据库，也不会自己查询用户衣橱。

后端负责：

- 查询当前用户的真实衣橱；
- 校验用户权限、锁定商品和排除商品；
- 筛选候选衣物；
- 把候选衣物的文本和图片发送给搭配模型；
- 对模型返回的商品 ID 再次执行 Java 硬规则校验；
- 创建并保存最终穿搭方案。

模型负责：

- 判断是否需要调用工具；
- 选择要调用的工具；
- 提供工具参数，例如 `scene`；
- 从后端提供的候选衣物中做颜色、风格、材质和版型等软性搭配判断；
- 根据工具返回结果生成自然语言回复。

可以概括为：

```text
模型负责软判断
Java 后端负责权限、硬规则、持久化和最终正确性
```

## 二、工具定义在哪里

工具定义在：

`backend/src/main/java/com/wardrobe/agent/agent/WardrobeAgentTools.java`

目前主要有两个工具：

- `generateOutfit(scene)`：生成一套穿搭；
- `getCurrentDateTime()`：获取服务器当前时间。

核心代码：

```java
@Tool(description = "仅当用户明确要求生成、推荐、调整穿搭或换一套时调用。")
public OutfitToolResult generateOutfit(
        @ToolParam(description = "从当前消息和会话上下文提取的完整穿搭场景与约束") String scene) {
    if (outfitResult == null) outfitResult = outfitGenerator.generate(scene);
    return outfitResult;
}
```

`@Tool` 和 `@ToolParam` 会被 Spring AI 自动转换成发送给模型的工具描述，形式大致如下：

```json
{
  "type": "function",
  "function": {
    "name": "generateOutfit",
    "description": "仅当用户明确要求生成、推荐、调整穿搭或换一套时调用。",
    "parameters": {
      "type": "object",
      "properties": {
        "scene": {
          "type": "string",
          "description": "从当前消息和会话上下文提取的完整穿搭场景与约束"
        }
      },
      "required": ["scene"]
    }
  }
}
```

这段工具 JSON 不是手写的，而是 Spring AI 根据 Java 方法和注解生成的。

## 三、`scene` 参数是不是动态的

是动态的。

它由模型根据以下内容提取和整理：

- 当前用户消息；
- 当前会话历史；
- 系统提示词；
- 当前运行时 `action`；
- 工具描述。

例如用户输入：

```text
帮我搭一套明天去上海出差的通勤穿搭，温度 18 度，不要黑色外套
```

模型可能返回：

```json
{
  "name": "generateOutfit",
  "arguments": {
    "scene": "明天上海出差，18 度，通勤场景，不要黑色外套"
  }
}
```

也可能返回语义相同但措辞不同的字符串。后端不会依赖固定措辞，而是把它作为本轮的穿搭场景。

需要注意：`@ToolParam` 主要提供参数名、类型和描述，并不会自动校验场景中必须包含地点、温度或某些关键词。真正的业务判断仍在 `AgentService` 中完成。

## 四、一次完整的 Tool Calling 流程

### 1. 请求进入 Agent

前端向以下接口提交一轮消息：

```text
POST /api/agent/conversations/{conversationId}/runs
```

入口在：

`backend/src/main/java/com/wardrobe/agent/agent/AgentController.java`

请求中除了用户文本，还可能包含：

```json
{
  "clientMessageId": "msg-001",
  "content": "帮我搭一套通勤穿搭",
  "lockedItemIds": ["item-101"],
  "excludedItemIds": ["item-205"],
  "action": null,
  "sourceOutfitId": null
}
```

其中：

- `content`：用户原始消息；
- `lockedItemIds`：用户要求必须保留的商品；
- `excludedItemIds`：用户要求不要使用的商品；
- `action=EXPLORE_MORE`：表示换一套；
- `sourceOutfitId`：换一套时的来源方案。

这些字段来自前端请求，不是 `ToolParam`。

### 2. 后端创建本轮工具对象

`AgentService.process()` 为本轮请求创建一个新的 `WardrobeAgentTools`：

```java
WardrobeAgentTools tools = new WardrobeAgentTools(
    scene -> generateOutfit(userId, conversationId, request, scene, emit)
);
```

这个 Lambda 把模型传入的 `scene` 连接到当前用户、当前会话的后端业务逻辑。

### 3. 把工具注册给对话模型

在：

`backend/src/main/java/com/wardrobe/agent/agent/SpringConversationAgentGateway.java`

代码通过以下方式注册工具：

```java
var request = chatClient.prompt()
        .messages(history)
        .messages(new SystemMessage(...))
        .user(content)
        .tools(tools);
```

对话模型此时能看到工具名称、描述和参数结构，但还没有直接看到完整衣橱数据库。

### 4. 模型返回工具调用

如果用户确实提出穿搭需求，模型会返回类似：

```json
{
  "name": "generateOutfit",
  "arguments": {
    "scene": "通勤场景"
  }
}
```

Spring AI 会根据这个工具调用自动执行：

```java
generateOutfit("通勤场景");
```

## 五、`generateOutfit` 工具内部如何取得衣服数据

真正业务逻辑在：

`backend/src/main/java/com/wardrobe/agent/agent/AgentService.java`

### 1. 确定最终场景

普通生成时，优先使用模型提供的 `scene`：

```java
String scene = sourcePlan == null
        ? safeText(requestedScene, request.content(), 500)
        : sourcePlan.getScene();
```

如果是 `EXPLORE_MORE`，后端会优先使用来源方案的原始场景，保持“换一套”时的上下文一致。

### 2. 查询当前用户衣橱

```java
List<WardrobeItem> all =
    wardrobe.findAllByUserIdAndDeletedFalseOrderByCreatedAtAsc(userId);
```

这里是后端通过 Repository 查询数据库，不是模型查询数据库。

### 3. 校验锁定和排除条件

后端会检查：

- 锁定商品是否属于当前用户；
- 锁定和排除是否同时包含同一商品；
- 被锁定的商品是否发生 slot 冲突；
- 排除商品不能进入候选集。

```java
validateConstraints(locked, request.safeExcluded(), byId);
```

### 4. 生成合法候选集

```java
List<WardrobeItem> eligible = all.stream()
        .filter(item -> !request.safeExcluded().contains(item.getId()))
        .toList();
```

接着调用语义检索：

```java
WardrobeRetrievalResult retrieval = semanticRetriever.retrieve(
    new WardrobeRetrievalRequest(...)
);
```

对应：

`backend/src/main/java/com/wardrobe/agent/retrieval/WardrobeSemanticRetriever.java`

语义检索会：

1. 过滤不属于当前用户、已删除或被排除的商品；
2. 根据场景生成查询向量；
3. 和衣物向量计算余弦相似度；
4. 按 slot 召回候选，避免候选全部集中在某一个类别；
5. 用 MMR 增加候选多样性；
6. 强制保留锁定商品和必要位置。

它只负责缩小候选范围，不直接决定最终穿搭。

## 六、搭配模型如何看到衣服数据

最终候选衣物会传给：

`backend/src/main/java/com/wardrobe/agent/agent/SpringAiGateway.java`

### 1. 转成候选文本

每件候选衣物会被整理为类似这样的文本：

```text
itemId=item-101 | image=第1张 | slot=INNER_TOP |
name=浅蓝色棉质衬衫 | color=浅蓝 | material=棉 |
scene=[通勤, 日常] | style=[简约, 休闲] | locked=false
```

代码中包含的字段有：

- `itemId`：商品唯一 ID；
- `slot`：衣物位置，例如上衣、下装、鞋子；
- `name`：名称；
- `color`：颜色；
- `material`：材质；
- `scene`：适用场景标签；
- `style`：风格标签；
- `locked`：是否为锁定商品。

### 2. 附加衣物图片

`WardrobeVisionService` 会读取受控图片资源，并进行：

- 图片所有权校验；
- 路径校验；
- 缩放；
- JPEG 压缩；
- 图片数量和尺寸限制。

然后通过：

```java
user.media(image.mimeType(), image.resource());
```

把图片作为同一条多模态 UserMessage 的内容发送给模型。

模型可以通过图片辅助判断：

- 实际颜色；
- 轮廓；
- 版型；
- 图案；
- 视觉协调性。

但系统提示词明确规定，模型不能仅凭图片猜测品牌、尺码或不可见属性。

### 3. 发送给搭配模型的 Prompt

搭配模型收到的信息大致是：

```text
场景：上海出差通勤，18 度

这是第 0 个备选方案。
新方案不得与已展示组合完全相同。

候选商品：
itemId=item-101 | slot=INNER_TOP | ...
itemId=item-205 | slot=BOTTOM | ...
itemId=item-309 | slot=SHOES | ...

请返回 title、selectedItemIds、reason。
selectedItemIds 只能放候选列表中的 itemId。
```

对应代码使用：

```java
.param("scene", scene)
.param("candidates", candidateText)
.media(...)
```

## 七、搭配模型返回什么

搭配模型不会返回完整商品对象，而是返回结构化的 `AiProposal`，例如：

```json
{
  "title": "轻商务通勤",
  "selectedItemIds": [
    "item-101",
    "item-205",
    "item-309"
  ],
  "reason": "浅蓝衬衫搭配深灰直筒裤，整体适合通勤场景，鞋子保持简洁稳重。"
}
```

模型只提出建议，不能直接写数据库。

## 八、Java 对模型结果做最终校验

后端会在 `normalizeSelection()` 中再次校验模型返回的 ID：

```java
Map<String, WardrobeItem> allowed = candidates.stream()
        .collect(Collectors.toMap(WardrobeItem::getId, item -> item));
```

只有 `allowed` 中的 ID 才可能被采用。

后端还会检查：

- 模型是否返回了不存在的商品；
- 是否保留锁定商品；
- 是否存在同 slot 重复；
- 连衣裙是否和上下装冲突；
- 是否缺少鞋子、上衣或下装；
- “换一套”是否与历史组合重复。

因此，即使模型返回：

```json
{
  "selectedItemIds": ["fake-item-999"]
}
```

后端也不会把这个商品写入穿搭方案。

通过校验后，后端才创建：

- `OutfitPlan`；
- `OutfitPlanItem`；
- `RecommendationRun`。

## 九、工具结果如何回到对话模型

`AgentService.generateOutfit()` 最终返回：

```java
new WardrobeAgentTools.OutfitToolResult(
    "CREATED",
    message,
    planView
);
```

Spring AI 会把这个工具结果交回原来的对话模型。对话模型再生成最终回复，例如：

```text
已为你生成一套轻商务通勤穿搭：浅蓝衬衫、深灰直筒裤和简洁运动鞋。
```

所以一次生成穿搭可能包含两类模型调用：

```text
第一次：对话模型决定是否调用工具，并生成 scene

第二次：搭配模型根据后端提供的候选衣物和图片选择 itemId
```

两个调用可以使用同一个底层模型，但职责不同、Prompt 不同。

## 十、面试时可以这样总结

> 我们没有让大模型直接访问数据库。对话模型先通过 Tool Calling 决定是否调用 `generateOutfit`，并把用户需求提取成 `scene`。工具执行后，后端根据当前用户权限、锁定和排除条件查询衣橱，再通过语义检索缩小候选范围。随后 `SpringAiGateway` 把候选衣物的 itemId、slot、名称、颜色、材质、标签和受控图片发送给搭配模型。搭配模型只返回候选商品的 itemId 组合和理由，后端再通过白名单、slot 冲突、锁定商品和完整性规则进行二次校验，最后持久化穿搭方案。也就是说，模型负责软性搭配判断，Java 负责权限、硬规则和最终落库。

