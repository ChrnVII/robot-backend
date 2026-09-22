## 结构说明

|层级|目录|职责|
|---|---|---|
|路由层|`app/api/`|定义 HTTP 接口、参数校验、依赖注入|
|核心层|`app/core/`|配置、数据库、安全、日志、异常等基础设施|
|数据层|`app/crud/`|封装数据库增删改查，不包含业务规则|
|模型层|`app/models/`|SQLAlchemy ORM 映射|
|校验层|`app/schemas/`|Pydantic 出入参定义与序列化|
|业务层|`app/services/`|编排业务逻辑（如注册发邮件、初始化配额）|

**调用方向**：`endpoint → service → crud → model`

> 如果业务非常薄，可以省略 `services/`，让 endpoint 直接调用 crud；当出现多步骤编排逻辑时，再引入 service 层。

