import * as z from "zod/mini";

// POC: Smarty Pants agent-rendered content block.
//
// We keep this schema permissive and versioned so we can iterate quickly.
export const sp_ai_widget_extra_data_schema = z.object({
    version: z.optional(z.int().check(z.nonnegative())),
    display: z.optional(z.enum(["card_only", "card_with_caption"])),
    title: z.optional(z.string()),
    caption: z.optional(z.string()),
    status: z.optional(z.enum(["running", "ok", "error"])),
    tool: z.optional(z.string()),
    input: z.optional(z.string()),
    output: z.optional(z.string()),
    // Rich, structured UI payload. This is intentionally optional so older payloads
    // that only populate tool/input/output keep rendering unchanged.
    turn: z.optional(
        z.object({
            kind: z.optional(z.string()),
            title: z.optional(z.string()),
            // Blocks are validated selectively in the widget code (we only render
            // the shapes we recognize).
            blocks: z.optional(z.array(z.unknown())),
        }),
    ),
});

export const sp_ai_subagent_schema = z.object({
    // Keep these permissive; the widget will apply defaults if fields are absent.
    type: z.optional(z.string()),
    description: z.optional(z.string()),
    status: z.optional(z.string()),
    toolCount: z.optional(z.int().check(z.nonnegative())),
    totalTokens: z.optional(z.int().check(z.nonnegative())),
    durationMs: z.optional(z.int().check(z.nonnegative())),
    agentURL: z.optional(z.string()),
    model: z.optional(z.string()),
    error: z.optional(z.string()),
});

export const sp_ai_subagent_group_block_schema = z.object({
    kind: z.literal("subagent_group"),
    title: z.optional(z.string()),
    // Optional for backward compatibility; when missing, the widget should fall
    // back to the existing text/list rendering.
    agents: z.optional(z.array(sp_ai_subagent_schema)),
    // Optional legacy fallback (not required by the spec, but lets us display a
    // plain-text representation when present).
    text: z.optional(z.string()),
});

export type SpAiWidgetExtraData = z.infer<typeof sp_ai_widget_extra_data_schema>;

export const sp_ai_widget_inbound_event_schema = z.discriminatedUnion("type", [
    z.object({type: z.literal("set_status"), status: z.enum(["running", "ok", "error"])}),
    z.object({type: z.literal("set_output"), output: z.string()}),
    z.object({type: z.literal("append_output"), chunk: z.string()}),
]);

export type SpAiWidgetInboundEvent = z.infer<typeof sp_ai_widget_inbound_event_schema>;
