import * as z from "zod/mini";

export type SpAiWidgetOutboundData = {
    type: "action";
    action: "abort" | "retry" | "approve" | "deny";
};

// Agent-native content block (sp_ai widget).
//
// Keep this schema permissive and versioned so we can iterate quickly without
// crashing the message list when payloads evolve.

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

export const sp_ai_subagent_group_block_schema = z.catchall(
    z.object({
        kind: z.literal("subagent_group"),
        title: z.optional(z.string()),
        // Optional for backward compatibility; when missing, the widget should fall
        // back to the existing text/list rendering.
        agents: z.optional(z.array(sp_ai_subagent_schema)),
        // Optional legacy fallback (not required by the spec, but lets us display a
        // plain-text representation when present).
        text: z.optional(z.string()),
    }),
    z.unknown(),
);

export const sp_ai_background_task_schema = z.catchall(
    z.object({
        // Keep this permissive; ids may be missing or numeric in early payloads.
        id: z.optional(z.string()),
        description: z.optional(z.string()),
        command: z.optional(z.string()),
        status: z.optional(z.string()),
        runtimeSec: z.optional(z.number()),
        outputPreview: z.optional(z.string()),
        exitCode: z.optional(z.number()),
        error: z.optional(z.string()),
    }),
    z.unknown(),
);

export const sp_ai_background_tasks_block_schema = z.catchall(
    z.object({
        // Support both names while we iterate on the upstream payload.
        kind: z.union([z.literal("background_tasks"), z.literal("bash_tasks")]),
        title: z.optional(z.string()),
        // Optional so the widget can safely fall back to generic rendering.
        tasks: z.optional(z.array(sp_ai_background_task_schema)),
    }),
    z.unknown(),
);

const sp_ai_turn_tool_schema = z.catchall(
    z.object({
        name: z.string(),
        callId: z.optional(z.string()),
        stepId: z.optional(z.string()),
        lettaRunId: z.optional(z.string()),
        argsText: z.optional(z.string()),
    }),
    z.unknown(),
);

const sp_ai_turn_parallel_schema = z.catchall(
    z.object({
        groupId: z.string(),
        index: z.optional(z.number()),
        count: z.optional(z.number()),
        hasPrev: z.optional(z.boolean()),
        hasNext: z.optional(z.boolean()),
    }),
    z.unknown(),
);

const sp_ai_known_turn_block_schema = z.union([
    z.catchall(
        z.object({
            kind: z.literal("markdown"),
            title: z.optional(z.string()),
            text: z.string(),
        }),
        z.unknown(),
    ),
    z.catchall(
        z.object({
            kind: z.literal("text"),
            title: z.optional(z.string()),
            text: z.string(),
        }),
        z.unknown(),
    ),
    z.catchall(
        z.object({
            kind: z.literal("code"),
            title: z.optional(z.string()),
            code: z.string(),
            language: z.optional(z.string()),
        }),
        z.unknown(),
    ),
    z.catchall(
        z.object({
            kind: z.literal("diff"),
            title: z.optional(z.string()),
            diff: z.string(),
            language: z.optional(z.string()),
        }),
        z.unknown(),
    ),
    z.catchall(
        z.object({
            kind: z.literal("json"),
            title: z.optional(z.string()),
            json: z.optional(z.unknown()),
            text: z.optional(z.string()),
        }),
        z.unknown(),
    ),
    z.catchall(
        z.object({
            kind: z.literal("table"),
            title: z.optional(z.string()),
            columns: z.optional(z.array(z.string())),
            rows: z.optional(z.array(z.array(z.string()))),
        }),
        z.unknown(),
    ),
    z.catchall(
        z.object({
            kind: z.literal("stream"),
            title: z.optional(z.string()),
            channel: z.enum(["stdout", "stderr"]),
            text: z.string(),
        }),
        z.unknown(),
    ),
    sp_ai_subagent_group_block_schema,
    sp_ai_background_tasks_block_schema,
]);

const sp_ai_unknown_turn_block_schema = z.catchall(
    z.object({
        kind: z.optional(z.string()),
        title: z.optional(z.string()),
        text: z.optional(z.string()),
    }),
    z.unknown(),
);

const sp_ai_turn_block_schema = z.union([
    sp_ai_known_turn_block_schema,
    sp_ai_unknown_turn_block_schema,
]);

const sp_ai_turn_schema = z.catchall(
    z.object({
        kind: z.enum([
            "thinking",
            "tool",
            "final",
            "error",
            "plan",
            "decision",
            "ask",
            "budget",
            "policy",
            "subagent_group",
        ]),
        status: z.enum([
            "pending",
            "running",
            "ok",
            "error",
            "aborted",
            "denied",
            "approval_requested",
            "approval_responded",
        ]),
        title: z.string(),
        subtitle: z.optional(z.string()),
        output: z.optional(z.string()),
        tool: z.optional(sp_ai_turn_tool_schema),
        parallel: z.optional(sp_ai_turn_parallel_schema),
        blocks: z.optional(z.array(sp_ai_turn_block_schema)),
    }),
    z.unknown(),
);

export const sp_ai_widget_extra_data_schema = z.catchall(
    z.object({
        // v1 fields (POC)
        version: z.optional(z.int().check(z.nonnegative())),
        display: z.optional(z.enum(["card_only", "card_with_caption"])),
        title: z.optional(z.string()),
        caption: z.optional(z.string()),
        status: z.optional(
            z.enum([
                "pending",
                "running",
                "ok",
                "error",
                "aborted",
                "denied",
                "approval_requested",
                "approval_responded",
            ]),
        ),
        tool: z.optional(z.string()),
        input: z.optional(z.string()),
        output: z.optional(z.string()),

        // v2 fields (turns)
        turn: z.optional(sp_ai_turn_schema),
    }),
    z.unknown(),
);

export type SpAiWidgetExtraData = z.infer<typeof sp_ai_widget_extra_data_schema>;

export const sp_ai_widget_inbound_event_schema = z.discriminatedUnion("type", [
    // v2: replace extra_data wholesale (used to morph thinking -> tool/final).
    z.object({type: z.literal("set_extra_data"), extra_data: z.unknown()}),

    // v1: direct mutations.
    z.object({
        type: z.literal("set_status"),
        status: z.enum([
            "pending",
            "running",
            "ok",
            "error",
            "aborted",
            "denied",
            "approval_requested",
            "approval_responded",
        ]),
    }),
    z.object({type: z.literal("set_output"), output: z.string()}),
    z.object({type: z.literal("append_output"), chunk: z.string()}),
]);

export type SpAiWidgetInboundEvent = z.infer<typeof sp_ai_widget_inbound_event_schema>;
