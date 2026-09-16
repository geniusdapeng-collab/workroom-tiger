import type { BusinessAdapterRegistration } from "../service/adapters/business.js";

/** 模拟盘服务前台不读取交易内核私有数据；未登记适配器时按基座规则失败关闭。 */
export const BUNDLED_BUSINESS_ADAPTERS: readonly BusinessAdapterRegistration[] = Object.freeze([]);
