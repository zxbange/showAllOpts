# 前端质量门禁

需要 Node.js 22.13 或更高的 22.x LTS 版本，或 Node.js 24+（CI 使用 Node.js 22）。在 `frontend/` 安装锁定的依赖后执行：

```bash
npm ci
npm run quality:check
npm test
npm run build
npm run test:e2e
```

`quality:check` 必须依次通过以下检查：

- `quality:format`：使用 Prettier 检查全部 `src/` 及 ESLint 配置与前端 package.json。原有 `format:check` 仍可用于检查整个前端目录，包括浏览器测试和运维脚本。
- `lint`：检查全部 `src/**/*.ts`、`src/**/*.tsx`，包括已有单元测试，禁止 debugger、新增显式 any、违反 React Hooks 调用规则以及不完整的 Hook 依赖。
- `type:check`：使用项目自身的严格 TypeScript 配置检查两套 TS 工程，不产生应用构建产物。

`eslint-suppressions.json` 是历史问题的文件/规则计数基线，仅保留高股息采集控件中有意用于过期请求淘汰的 ref 清理警告，以及大宗商品图表的三个旧 any。它不是目录豁免。超过既有计数或出现新文件/新规则违规会失败；旧问题减少时 ESLint 也会要求清理对应基线。修复时运行 `npm run lint -- --prune-suppressions`，检查差异后提交，禁止用自动重新生成基线掩盖新增问题。新规则引入时单独评审其基线。该门禁与业务测试互补，不替代浏览器测试或数据服务验证。
