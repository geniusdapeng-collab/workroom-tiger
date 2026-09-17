import { describe, expect, it } from "vitest";
import { appRouter } from "./router.js";

describe("三端访问权威路由面", () => {
  it("挂载 access.me，且匿名请求失败关闭", async () => {
    const caller = appRouter.createCaller({
      session: null,
      identity: null,
      partnerIdentity: null,
      headers: new Headers(),
    });

    await expect(caller.access.me()).rejects.toMatchObject({ code: "UNAUTHORIZED" });
  });

  it("挂载首次欢迎状态、进度保存与重播端点", () => {
    const procedures = (appRouter as unknown as {
      _def: { procedures: Record<string, unknown> };
    })._def.procedures;
    expect(procedures).toHaveProperty("onboarding.welcomeStatus");
    expect(procedures).toHaveProperty("onboarding.saveWelcomeProgress");
    expect(procedures).toHaveProperty("onboarding.replayWelcome");
  });
});
