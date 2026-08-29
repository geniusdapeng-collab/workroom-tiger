/**
 * apps/server 最小入口（A6）：Hono + tRPC v11（fetch adapter）+ 健康检查
 * 端口：SERVER_PORT（默认 8787，见 .env.example）
 * 纪律：中间件栈（鉴权/租户解析/版本能力 403/错误规约）在阶段二 B5 挂载；
 *      本卡只保证「起得来、握得上、查得到 DB」。
 */
import { serve } from "@hono/node-server";
import { fetchRequestHandler } from "@trpc/server/adapters/fetch";
import { Hono } from "hono";
import { cors } from "hono/cors";
import { appRouter } from "./trpc/router.js";
import { createContext } from "./trpc/context.js";

const app = new Hono();

app.use(
  "*",
  cors({
    origin: (origin) => origin ?? "*", // 本地开发：vite dev server 代理外也允许直连
    credentials: true,
  }),
);

/** 裸健康检查（不进 tRPC，供 start.sh/编排探活） */
app.get("/health", (c) => c.json({ ok: true, service: "workloom-im-server" }));

/** tRPC v11 over HTTP（fetch adapter；httpBatchLink 由客户端侧决定） */
app.all("/trpc/*", async (c) => {
  const res = await fetchRequestHandler({
    endpoint: "/trpc",
    req: c.req.raw,
    router: appRouter,
    createContext: () => createContext(c.req.raw),
  });
  return res;
});

const port = Number(process.env.SERVER_PORT ?? 8787);
serve({ fetch: app.fetch, port }, (info) => {
  console.log(`WorkLoom IM 底座 server 已启动：http://localhost:${info.port}（tRPC: /trpc/*）`);
});
