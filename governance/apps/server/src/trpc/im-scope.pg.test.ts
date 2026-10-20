/**
 * MC-029: actual tRPC + PostgreSQL RLS, with in-memory IM transport only.
 * Opt in on a migrated loopback workloom_tiger_* test database. This suite
 * changes DB grants/triggers and process stubs, so run its file serially.
 */
import { randomUUID } from "node:crypto";
import pg from "pg";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { closeAllPools, getAppPool, getGatewayPool } from "@workloom/db";
import type { Identity } from "@workloom/base/tenancy";
import { MockChannelDriver } from "@workloom/base/im-channels";
import { gatewayAppend } from "@workloom/base/workdata";

const suite = process.env.RUN_IM_SCOPE_PG_TESTS === "1" ? describe : describe.skip;
const channels = ["dingtalk", "wecom", "feishu"] as const;
type Channel = (typeof channels)[number];
type Fixture = { identity: Identity; openId: string };
type Approval = { approvalId: string; eventId: string; conversationId: string; before: string; after: string };
const uid = () => randomUUID().replaceAll("-", "");
let appRouter: typeof import("./router.js").appRouter;

function caller(identity: Identity | null, headers = new Headers()) {
  return appRouter.createCaller({ identity, session: identity, partnerIdentity: null, headers });
}

suite("MC-029 mock IM outbox scope", () => {
  let owner: pg.Pool;
  const fetchAttempt = vi.fn(async () => { throw new Error("MC029 forbids external HTTP requests"); });

  async function member(scope: Pick<Identity, "tenantId" | "workspaceId">, role: "owner" | "readonly" = "owner"): Promise<Fixture> {
    const memberId = `mc029-member-${uid()}`, memberNo = `MC029-${uid()}`, openId = `mc029-open-${uid()}`;
    await owner.query(
      "INSERT INTO members (id,workspace_id,member_no,name,role,im_openids) VALUES ($1,$2,$3,'MC029 synthetic member',$4,$5)",
      [memberId, scope.workspaceId, memberNo, role, JSON.stringify(Object.fromEntries(channels.map((channel) => [channel, openId])))],
    );
    return { identity: { ...scope, memberId, memberNo, role, name: "MC029 synthetic member", plan: "pro" }, openId };
  }

  async function fixture(input: { tenantId?: string; workspaceId?: string; role?: "owner" | "readonly" } = {}): Promise<Fixture> {
    const tenantId = input.tenantId ?? `mc029-tenant-${uid()}`;
    const workspaceId = input.workspaceId ?? `mc029-workspace-${uid()}`;
    await owner.query("INSERT INTO tenants (id,name,plan) VALUES ($1,'MC029 synthetic tenant','pro') ON CONFLICT(id) DO NOTHING", [tenantId]);
    await owner.query(
      "INSERT INTO workspaces (id,tenant_id,name,slug,industry) VALUES ($1,$2,'MC029 synthetic workspace',$1,'test')",
      [workspaceId, tenantId],
    );
    return member({ tenantId, workspaceId }, input.role);
  }

  async function approval(subject: Fixture, expiresAt = new Date(Date.now() + 3600_000).toISOString()): Promise<Approval> {
    const before = `MC029_BEFORE_${uid()}`, after = `MC029_AFTER_${uid()}`;
    const { identity } = subject;
    const event = await gatewayAppend(getGatewayPool(), { ...identity, actor: { id: identity.memberNo, type: "human" } }, {
      who: { id: identity.memberNo, type: "human" },
      context: { tenant_id: identity.tenantId, workspace_id: identity.workspaceId, time: new Date().toISOString() },
      object: { type: "record", id: `mc029-record-${uid()}`, label: "MC029 synthetic record" },
      decision: { action: "mc029.synthetic.read", before: { marker: before }, after: { marker: after } },
      rule_impact: [],
    });
    const approvalId = `mc029-approval-${uid()}`;
    await owner.query(
      `INSERT INTO approvals (approval_id,tenant_id,workspace_id,event_id,channel,status,snapshot)
       VALUES ($1,$2,$3,$4,'inapp','pending',$5)`,
      [approvalId, identity.tenantId, identity.workspaceId, event.eventId, JSON.stringify({ expires_at: expiresAt })],
    );
    return { approvalId, eventId: event.eventId, conversationId: `mc029-conversation-${uid()}`, before, after };
  }

  function send(subject: Fixture, card: Approval, channel: Channel = "wecom") {
    return caller(subject.identity).im.sendApprovalCard({ approvalId: card.approvalId, conversationId: card.conversationId, channel });
  }

  function callback(subject: Fixture, card: Approval, channel: Channel = "wecom") {
    return caller(subject.identity).im.callback({
      channel, approvalId: card.approvalId, operatorOpenId: subject.openId,
      conversationId: card.conversationId, gesture: "approve",
    });
  }

  async function outbox(subject: Fixture, channel: Channel = "wecom") {
    // createCaller returns references directly; snapshot the actual response as
    // HTTP serialization would, so subsequent sends cannot mutate prior evidence.
    const response = await caller(subject.identity).im.outbox({ channel });
    expect(response.driver).toBe("mock");
    return structuredClone(response.outbox);
  }

  async function audit(subject: Fixture, action: string) {
    return (await owner.query(
      "SELECT tenant_id,workspace_id,payload FROM biz_events WHERE workspace_id=$1 AND payload->'decision'->>'action'=$2 ORDER BY seq",
      [subject.identity.workspaceId, action],
    )).rows;
  }

  async function status(card: Approval) {
    return (await owner.query("SELECT status,gesture,decided_by,consumed_at FROM approvals WHERE approval_id=$1", [card.approvalId])).rows[0];
  }

  beforeAll(async () => {
    const urls = ["DATABASE_URL", "DATABASE_APP_URL", "DATABASE_GATEWAY_URL"].map((key) => new URL(process.env[key] ?? "http://invalid"));
    if (urls.some((url) => !["postgres:", "postgresql:"].includes(url.protocol)
      || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
      || !/^\/workloom_tiger_[a-z0-9_]+$/.test(url.pathname)
      || url.hostname !== urls[0]!.hostname || url.port !== urls[0]!.port || url.pathname !== urls[0]!.pathname)) {
      throw new Error("MC029 requires three roles on the same loopback workloom_tiger_ test database");
    }
    vi.stubEnv("LLM_PROVIDER", "mock");
    vi.stubEnv("IM_DRIVER", "mock");
    vi.stubEnv("ASK_WEB_SEARCH", "0");
    for (const channel of channels) vi.stubEnv(`IM_CHANNEL_SECRET_${channel.toUpperCase()}`, "");
    vi.stubGlobal("fetch", fetchAttempt);
    owner = new pg.Pool({ connectionString: urls[0]!.toString(), connectionTimeoutMillis: 5000, statement_timeout: 10000 });
    for (const [pool, role] of [[getAppPool(), "workloom_app"], [getGatewayPool(), "workloom_gateway"]] as const) {
      expect((await pool.query("SELECT current_user,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")).rows[0])
        .toEqual({ current_user: role, rolsuper: false, rolbypassrls: false });
    }
    appRouter = (await import("./router.js")).appRouter;
  });

  afterEach(() => {
    expect(fetchAttempt).not.toHaveBeenCalled();
    vi.restoreAllMocks();
  });

  afterAll(async () => {
    await Promise.all([owner?.end(), closeAllPools()]);
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("returns an empty mock outbox for a scope that has never sent", async () => {
    const subject = await fixture();
    for (const channel of channels) expect(await outbox(subject, channel)).toEqual([]);
  });

  it.each(channels)("keeps complete %s cards readable by other authorized members of the same workspace", async (channel) => {
    const subject = await fixture(), reader = await member(subject.identity, "readonly");
    const card = await approval(subject);
    const sent = await send(subject, card, channel);
    expect(sent.channelMsgId).toMatch(new RegExp(`^mock-${channel}-`));
    expect(await outbox(reader, channel)).toEqual([{
      kind: "card", target: { conversationId: card.conversationId }, channelMsgId: sent.channelMsgId,
      card: sent.card,
    }]);
    expect(sent.card).toMatchObject({
      approvalId: card.approvalId, eventId: card.eventId,
      before: { marker: card.before }, after: { marker: card.after },
      gestures: ["approve", "edit", "reject"],
    });
    expect(await audit(subject, "approval.card.sent")).toEqual([expect.objectContaining({
      tenant_id: subject.identity.tenantId, workspace_id: subject.identity.workspaceId,
      payload: expect.objectContaining({ decision: expect.objectContaining({
        after: expect.objectContaining({ approval_id: card.approvalId, channel_msg_id: sent.channelMsgId }),
      }) }),
    })]);
    expect(await status(card)).toMatchObject({ status: "pending", consumed_at: null });
  });

  it.each(channels)("isolates %s card payloads from another tenant and another workspace in the same tenant", async (channel) => {
    const subject = await fixture();
    const foreignTenant = await fixture({ role: "readonly" });
    const foreignWorkspace = await fixture({ tenantId: subject.identity.tenantId, role: "readonly" });
    const card = await approval(subject);
    await send(subject, card, channel);
    const own = await outbox(subject, channel);
    const responses = await Promise.all([outbox(foreignTenant, channel), outbox(foreignWorkspace, channel)]);
    const containsCard = (rows: typeof own) => rows.some((row) => row.card?.approvalId === card.approvalId);
    expect({
      foreignTenantReceivesOwnCard: containsCard(responses[0]!),
      sameTenantForeignWorkspaceReceivesOwnCard: containsCard(responses[1]!),
    }).toEqual({ foreignTenantReceivesOwnCard: false, sameTenantForeignWorkspaceReceivesOwnCard: false });
    for (const [index, outside] of [foreignTenant, foreignWorkspace].entries()) {
      const response = responses[index]!;
      for (const sentinel of [card.before, card.after, card.approvalId, card.eventId, card.conversationId]) {
        expect(JSON.stringify(response)).not.toContain(sentinel);
      }
      expect(response).toEqual([]);
      expect(await caller(outside.identity).approvals.list()).toEqual([]);
    }
    expect(own).toHaveLength(1);
    expect(await outbox(subject, channel)).toEqual(own);
  });

  it("keeps the three channels separate within one scope", async () => {
    const subject = await fixture();
    const cards = await Promise.all(channels.map(() => approval(subject)));
    await Promise.all(channels.map((channel, index) => send(subject, cards[index]!, channel)));
    for (const [index, channel] of channels.entries()) {
      expect((await outbox(subject, channel)).map((row) => row.card?.approvalId)).toEqual([cards[index]!.approvalId]);
    }
  });

  it.each([":", "|", "/", "\n", '","雪\\'])("does not collide when scope identifiers contain %j", async (separator) => {
    const prefix = `mc029-${uid()}`, suffix = `ws-${uid()}`;
    const a = await fixture({ tenantId: `${prefix}${separator}tail`, workspaceId: suffix });
    const b = await fixture({ tenantId: prefix, workspaceId: `tail${separator}${suffix}` });
    const aCard = await approval(a), bCard = await approval(b);
    await Promise.all([send(a, aCard), send(b, bCard)]);
    expect((await outbox(a)).map((row) => row.card?.approvalId)).toEqual([aCard.approvalId]);
    expect((await outbox(b)).map((row) => row.card?.approvalId)).toEqual([bCard.approvalId]);
  });

  it("derives send, callback and read scope from current identity despite extra client scope fields", async () => {
    const a = await fixture(), b = await fixture();
    const aCard = await approval(a), bCard = await approval(b);
    await send(a, aCard);
    const forged = { tenantId: a.identity.tenantId, workspaceId: a.identity.workspaceId, channel: "wecom" as const };
    await caller(b.identity).im.sendApprovalCard({ ...forged, approvalId: bCard.approvalId, conversationId: bCard.conversationId });
    await caller(b.identity).im.callback({
      ...forged, approvalId: bCard.approvalId, operatorOpenId: b.openId,
      conversationId: bCard.conversationId, gesture: "approve",
    });
    const read = await caller(b.identity).im.outbox(forged);
    expect(read.outbox).toHaveLength(2);
    expect(JSON.stringify(read)).not.toContain(aCard.approvalId);
    expect((await outbox(a)).map((row) => row.card?.approvalId)).toEqual([aCard.approvalId]);
    expect(await status(aCard)).toMatchObject({ status: "pending" });
    expect(await status(bCard)).toMatchObject({ status: "approved" });
  });

  it("requires authentication and current membership even when the requested scope has a cached outbox", async () => {
    const subject = await fixture(), card = await approval(subject), outside = await fixture();
    await send(subject, card);
    await expect(caller(null).im.outbox({ channel: "wecom" })).rejects.toMatchObject({ code: "UNAUTHORIZED" });
    await expect(caller({ ...outside.identity, tenantId: subject.identity.tenantId, workspaceId: subject.identity.workspaceId })
      .im.outbox({ channel: "wecom" })).rejects.toMatchObject({ code: "UNAUTHORIZED" });
    await owner.query("UPDATE members SET status='removed' WHERE id=$1", [subject.identity.memberId]);
    await expect(caller(subject.identity).im.outbox({ channel: "wecom" })).rejects.toMatchObject({ code: "UNAUTHORIZED" });
    await owner.query("UPDATE members SET status='active' WHERE id=$1", [subject.identity.memberId]);
    expect(await outbox(subject)).toHaveLength(1);
  });

  it("retains live readonly and explicit deny guards for sends and callbacks", async () => {
    const subject = await fixture(), card = await approval(subject);
    await owner.query("UPDATE members SET role='readonly' WHERE id=$1", [subject.identity.memberId]);
    await expect(send(subject, card)).rejects.toMatchObject({ code: "FORBIDDEN" });
    await expect(callback(subject, card)).rejects.toMatchObject({ code: "FORBIDDEN" });
    await owner.query("UPDATE members SET role='owner',permissions=$2 WHERE id=$1", [subject.identity.memberId, JSON.stringify({ deny: ["workspace.write"] })]);
    await expect(send(subject, card)).rejects.toMatchObject({ code: "FORBIDDEN" });
    await expect(callback(subject, card)).rejects.toMatchObject({ code: "FORBIDDEN" });
    expect(await outbox(subject)).toEqual([]);
    expect(await status(card)).toMatchObject({ status: "pending" });
    expect(await audit(subject, "approval.card.sent")).toEqual([]);
    expect(await audit(subject, "approval.gesture")).toEqual([]);
  });

  it("rejects missing and foreign approvals before writing to either outbox", async () => {
    const subject = await fixture();
    const outsiders = [await fixture(), await fixture({ tenantId: subject.identity.tenantId })];
    const card = await approval(subject);
    for (const outside of outsiders) {
      await expect(send(outside, card)).rejects.toMatchObject({ code: "NOT_FOUND" });
      await expect(callback(outside, card)).rejects.toMatchObject({ code: "BAD_REQUEST" });
      expect(await outbox(outside)).toEqual([]);
    }
    await expect(send(subject, { ...card, approvalId: "mc029-missing-approval" })).rejects.toMatchObject({ code: "NOT_FOUND" });
    expect(await outbox(subject)).toEqual([]);
    expect(await status(card)).toMatchObject({ status: "pending" });
  });

  it("rejects empty IDs, unsupported channels, wrong operators and invalid signatures without an acknowledgement", async () => {
    const subject = await fixture(), card = await approval(subject);
    await expect(send(subject, { ...card, conversationId: "" })).rejects.toMatchObject({ code: "BAD_REQUEST" });
    await expect(send(subject, { ...card, approvalId: "" })).rejects.toMatchObject({ code: "BAD_REQUEST" });
    await expect(caller(subject.identity).im.outbox({ channel: "slack" as Channel })).rejects.toMatchObject({ code: "BAD_REQUEST" });
    const input = { channel: "wecom" as const, approvalId: card.approvalId, operatorOpenId: subject.openId,
      conversationId: card.conversationId, gesture: "approve" as const };
    await expect(caller(subject.identity).im.callback({ ...input, operatorOpenId: "mc029-unbound" })).rejects.toMatchObject({ code: "FORBIDDEN" });
    vi.stubEnv("IM_CHANNEL_SECRET_WECOM", "mc029-synthetic-signature-key");
    try {
      const headers = new Headers({ "x-channel-timestamp": String(Math.floor(Date.now() / 1000)), "x-channel-signature": "invalid" });
      await expect(caller(subject.identity, headers).im.callback(input)).rejects.toMatchObject({ code: "FORBIDDEN" });
    } finally {
      vi.stubEnv("IM_CHANNEL_SECRET_WECOM", "");
    }
    expect(await outbox(subject)).toEqual([]);
    expect(await status(card)).toMatchObject({ status: "pending" });
  });

  it.each(["approve", "edit", "reject"] as const)("retains %s callbacks and confines their acknowledgement to the sending workspace", async (gesture) => {
    const subject = await fixture(), reader = await member(subject.identity, "readonly");
    const outside = await fixture(), sibling = await fixture({ tenantId: subject.identity.tenantId });
    const card = await approval(subject);
    await send(subject, card);
    const result = await caller(subject.identity).im.callback({
      channel: "wecom", approvalId: card.approvalId, operatorOpenId: subject.openId,
      conversationId: card.conversationId, gesture,
      ...(gesture === "edit" ? { editedAfter: { marker: "MC029 synthetic revision" } } : {}),
      ...(gesture === "reject" ? { reasonEnum: "mc029-synthetic-reason", reasonText: "Synthetic fixture only" } : {}),
    });
    const expectedStatus = gesture === "approve" ? "approved" : gesture === "edit" ? "edited" : "rejected";
    expect(result).toMatchObject({ status: expectedStatus, deduped: false, unsigned: true });
    const rows = await outbox(reader);
    expect(rows).toHaveLength(2);
    expect(rows[0]?.card?.approvalId).toBe(card.approvalId);
    expect(rows[1]).toMatchObject({ kind: "text", target: { conversationId: card.conversationId } });
    expect(rows[1]?.text).toContain(card.approvalId);
    expect(await status(card)).toMatchObject({ status: expectedStatus, decided_by: subject.identity.memberNo });
    expect(await audit(subject, "approval.gesture")).toHaveLength(1);
    expect(await outbox(outside)).toEqual([]);
    expect(await outbox(sibling)).toEqual([]);
    await expect(send(subject, card)).rejects.toMatchObject({ code: "NOT_FOUND" });
    expect(await outbox(subject)).toEqual(rows);
  });

  it("preserves callback validation and expiry without creating response messages", async () => {
    const subject = await fixture(), card = await approval(subject);
    const input = { channel: "wecom" as const, approvalId: card.approvalId, operatorOpenId: subject.openId, conversationId: card.conversationId };
    await expect(caller(subject.identity).im.callback({ ...input, gesture: "reject" })).rejects.toMatchObject({ code: "BAD_REQUEST" });
    await expect(caller(subject.identity).im.callback({ ...input, gesture: "edit" })).rejects.toMatchObject({ code: "BAD_REQUEST" });
    const expired = await approval(subject, new Date(Date.now() - 1000).toISOString());
    await expect(callback(subject, expired)).rejects.toMatchObject({ code: "BAD_REQUEST" });
    expect(await status(card)).toMatchObject({ status: "pending" });
    expect(await status(expired)).toMatchObject({ status: "expired" });
    expect(await outbox(subject)).toEqual([]);
  });

  it("keeps concurrent sends in their own scope with distinct message IDs per outbox", async () => {
    const a = await fixture(), b = await fixture(), c = await fixture({ tenantId: a.identity.tenantId });
    const fixtures = [a, b, c];
    const cards = await Promise.all(fixtures.map((subject) => Promise.all(Array.from({ length: 4 }, () => approval(subject)))));
    await Promise.all(fixtures.flatMap((subject, index) => cards[index]!.map((card) => send(subject, card))));
    for (const [index, subject] of fixtures.entries()) {
      const rows = await outbox(subject);
      expect(rows).toHaveLength(4);
      expect(new Set(rows.map((row) => row.channelMsgId)).size).toBe(4);
      expect(rows.map((row) => row.card?.approvalId).sort()).toEqual(cards[index]!.map((card) => card.approvalId).sort());
      expect(await audit(subject, "approval.card.sent")).toHaveLength(4);
    }
  });

  it("deduplicates concurrent callbacks while retaining all own acknowledgements", async () => {
    const subject = await fixture(), outside = await fixture(), card = await approval(subject);
    await send(subject, card);
    const results = await Promise.all(Array.from({ length: 5 }, () => callback(subject, card)));
    expect(results.filter((result) => !result.deduped)).toHaveLength(1);
    expect(results.filter((result) => result.deduped)).toHaveLength(4);
    expect(await audit(subject, "approval.gesture")).toHaveLength(1);
    const rows = await outbox(subject);
    expect(rows).toHaveLength(6);
    expect(rows.filter((row) => row.text?.includes("已处理过"))).toHaveLength(4);
    expect(await outbox(outside)).toEqual([]);
  });

  it("surfaces transport failure before any card or audit write and permits a scoped retry", async () => {
    const subject = await fixture(), outside = await fixture(), card = await approval(subject);
    const sendFailure = vi.spyOn(MockChannelDriver.prototype, "sendCard").mockRejectedValueOnce(new Error("MC029 synthetic send failure"));
    try {
      await expect(send(subject, card)).rejects.toMatchObject({ code: "INTERNAL_SERVER_ERROR" });
      expect(await outbox(subject)).toEqual([]);
      expect(await audit(subject, "approval.card.sent")).toEqual([]);
      expect(await status(card)).toMatchObject({ status: "pending" });
    } finally {
      sendFailure.mockRestore();
    }
    await send(subject, card);
    expect(await outbox(subject)).toHaveLength(1);
    expect(await outbox(outside)).toEqual([]);
  });

  it("keeps a committed approval successful when acknowledgement delivery fails", async () => {
    const subject = await fixture(), outside = await fixture(), card = await approval(subject);
    await send(subject, card);
    const ackFailure = vi.spyOn(MockChannelDriver.prototype, "sendText").mockRejectedValueOnce(new Error("MC029 synthetic acknowledgement failure"));
    try {
      expect(await callback(subject, card)).toMatchObject({ status: "approved", deduped: false });
    } finally {
      ackFailure.mockRestore();
    }
    expect(await status(card)).toMatchObject({ status: "approved" });
    expect(await outbox(subject)).toHaveLength(1);
    expect(await callback(subject, card)).toMatchObject({ status: "approved", deduped: true });
    expect((await outbox(subject)).at(-1)?.text).toContain("已处理过");
    expect(await outbox(outside)).toEqual([]);
  });

  it("propagates a database lookup failure without writing a card, then recovers", async () => {
    const subject = await fixture(), outside = await fixture(), card = await approval(subject);
    await owner.query("REVOKE SELECT ON approvals FROM workloom_app");
    try {
      await expect(send(subject, card)).rejects.toMatchObject({ code: "INTERNAL_SERVER_ERROR" });
      expect(await outbox(subject)).toEqual([]);
      expect(await audit(subject, "approval.card.sent")).toEqual([]);
    } finally {
      await owner.query("GRANT SELECT ON approvals TO workloom_app");
    }
    await send(subject, card);
    expect(await outbox(subject)).toHaveLength(1);
    expect(await outbox(outside)).toEqual([]);
  });

  it.each([false, true])("confines an already sent card when audit writing fails (compensation also fails: %s)", async (failCompensation) => {
    const subject = await fixture(), outside = await fixture(), card = await approval(subject);
    const trigger = `mc029_audit_failure_${uid()}`;
    await owner.query(`CREATE FUNCTION ${trigger}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'MC029 synthetic audit failure'; END $$`);
    try {
      const actions = failCompensation ? "'approval.card.sent','im.outbound.unrecorded'" : "'approval.card.sent'";
      await owner.query(`CREATE TRIGGER ${trigger} BEFORE INSERT ON biz_events FOR EACH ROW
        WHEN (NEW.workspace_id='${subject.identity.workspaceId}' AND NEW.payload->'decision'->>'action' IN (${actions}))
        EXECUTE FUNCTION ${trigger}()`);
      if (failCompensation) {
        await expect(send(subject, card)).rejects.toMatchObject({ code: "INTERNAL_SERVER_ERROR" });
        expect(await audit(subject, "im.outbound.unrecorded")).toEqual([]);
      } else {
        expect(await send(subject, card)).toMatchObject({ compensated: true });
        expect(await audit(subject, "im.outbound.unrecorded")).toHaveLength(1);
      }
      expect(await audit(subject, "approval.card.sent")).toEqual([]);
      expect((await outbox(subject)).map((row) => row.card?.approvalId)).toEqual([card.approvalId]);
      expect(await outbox(outside)).toEqual([]);
      expect(await status(card)).toMatchObject({ status: "pending" });
    } finally {
      await owner.query(`DROP TRIGGER IF EXISTS ${trigger} ON biz_events`);
      await owner.query(`DROP FUNCTION ${trigger}()`);
    }
  });
});
