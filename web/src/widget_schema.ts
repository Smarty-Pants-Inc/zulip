import * as z from "zod/mini";

import {poll_widget_extra_data_schema} from "./poll_data.ts";
import type {PollData, PollWidgetOutboundData} from "./poll_data.ts";
import {sp_ai_widget_extra_data_schema} from "./sp_ai_data.ts";
import type {SpAiWidgetExtraData, SpAiWidgetOutboundData} from "./sp_ai_data.ts";
import {todo_widget_extra_data_schema} from "./todo_data.ts";
import type {TaskData, TodoWidgetOutboundData} from "./todo_data.ts";
import {type ZFormExtraData, zform_widget_extra_data_schema} from "./zform_data.ts";

/*
    We can eventually unify this module with widget_data.ts, but until we can
    extract todo_data.ts and friends, we keep this small schema module to avoid
    circular dependencies.
*/

export type WidgetOutboundData = PollWidgetOutboundData | TodoWidgetOutboundData | SpAiWidgetOutboundData;

export const any_widget_data_schema = z.discriminatedUnion("widget_type", [
    z.object({widget_type: z.literal("poll"), extra_data: poll_widget_extra_data_schema}),
    z.object({
        widget_type: z.literal("zform"),
        extra_data: z.nullable(zform_widget_extra_data_schema),
    }),
    z.object({
        widget_type: z.literal("sp_ai"),
        extra_data: z.nullable(sp_ai_widget_extra_data_schema),
    }),
    z.object({
        widget_type: z.literal("todo"),
        extra_data: z.nullable(todo_widget_extra_data_schema),
    }),
]);

export type AnyWidgetData = z.infer<typeof any_widget_data_schema>;

export type WidgetData =
    | {
          widget_type: "todo";
          data: TaskData;
      }
    | {widget_type: "poll"; data: PollData}
    | {widget_type: "zform"; data: ZFormExtraData | undefined}
    | {widget_type: "sp_ai"; data: SpAiWidgetExtraData | undefined};
