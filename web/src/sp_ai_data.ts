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
});

export type SpAiWidgetExtraData = z.infer<typeof sp_ai_widget_extra_data_schema>;

export const sp_ai_widget_inbound_event_schema = z.discriminatedUnion("type", [
    z.object({type: z.literal("set_status"), status: z.enum(["running", "ok", "error"])}),
    z.object({type: z.literal("set_output"), output: z.string()}),
    z.object({type: z.literal("append_output"), chunk: z.string()}),
]);

export type SpAiWidgetInboundEvent = z.infer<typeof sp_ai_widget_inbound_event_schema>;
