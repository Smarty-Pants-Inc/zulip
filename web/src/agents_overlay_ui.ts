import $ from "jquery";

import render_agents_list from "../templates/agents_list.hbs";
import render_agents_overlay from "../templates/agents_overlay.hbs";
import render_memory_blocks_list from "../templates/memory_blocks_list.hbs";
import render_memory_blocks_panel from "../templates/memory_blocks_panel.hbs";

import * as browser_history from "./browser_history.ts";
import * as channel from "./channel.ts";
import {$t_html} from "./i18n.ts";
import * as location_memory_ui from "./location_memory_ui.ts";
import type {LocationMemoryContext} from "./location_memory_ui.ts";
import * as overlays from "./overlays.ts";
import * as ui_report from "./ui_report.ts";

export type AgentData = {
    id: number | string;
    name: string;
    // Backends vary; we ignore unknown keys.
    [key: string]: unknown;
};

type MemoryBlock = {
    id: string;
    label: string;
    value: string;
    [key: string]: unknown;
};

let overlay_is_open = false;
let current_memory_blocks_agent_id: string | null = null;

function get_error_box(): JQuery {
    return $("#agents_overlay_error");
}

function clear_error(): void {
    const $error = get_error_box();
    $error.removeClass("show alert-error alert-success").empty();
}

function show_client_error(message_html: string): void {
    ui_report.client_error(message_html, get_error_box());
}

function show_server_error(message_html: string, xhr: JQuery.jqXHR<unknown>): void {
    ui_report.error(message_html, xhr, get_error_box());
}

function show_success(message_html: string): void {
    ui_report.success(message_html, get_error_box(), 1200);
}

function escape_text_for_html(text: string): string {
    // ui_report.message uses .html() and expects callers to escape.
    // Use a jQuery text node to escape any server-provided error strings.
    return $("<div>").text(text).html();
}

function show_server_error_prefer_json_msg(
    fallback_message_html: string,
    xhr: JQuery.jqXHR<unknown>,
): void {
    const msg = get_xhr_json_message(xhr);
    if (msg !== undefined) {
        // This message is server-provided and not i18n-able.
        show_client_error(escape_text_for_html(msg));
        return;
    }
    show_server_error(fallback_message_html, xhr);
}

function extract_agents(data: unknown): AgentData[] {
    // Support either `{agents: [...]}` or a top-level array.
    if (data && typeof data === "object" && Array.isArray((data as {agents?: unknown}).agents)) {
        return ((data as {agents: unknown[]}).agents as unknown[])
            .filter((a) => a && typeof a === "object")
            .map((a) => {
                const agent = a as Record<string, unknown>;
                const id = agent["id"];
                const name = agent["name"];
                return {
                    id: (typeof id === "number" || typeof id === "string") && id !== "" ? id : "",
                    name: typeof name === "string" ? name : "(unnamed)",
                    ...agent,
                };
            })
            .filter((a) => a.id !== "");
    }

    if (Array.isArray(data)) {
        return data
            .filter((a) => a && typeof a === "object")
            .map((a) => {
                const agent = a as Record<string, unknown>;
                const id = agent["id"];
                const name = agent["name"];
                return {
                    id: (typeof id === "number" || typeof id === "string") && id !== "" ? id : "",
                    name: typeof name === "string" ? name : "(unnamed)",
                    ...agent,
                };
            })
            .filter((a) => a.id !== "");
    }

    return [];
}

function render_list(agents: AgentData[]): void {
    $("#agents-list").html(render_agents_list({agents}));
}

function set_loading_state(): void {
    $("#agents-list").html(
        `<div class="no-overlay-messages">${$t_html({defaultMessage: "Loading…"})}</div>`,
    );
}

export function load_agents(): void {
    if (!overlay_is_open) {
        return;
    }

    clear_error();
    set_loading_state();

    void channel.get({
        url: "/json/smarty_pants/agents",
        success(data) {
            render_list(extract_agents(data));
        },
        error(xhr) {
            show_server_error($t_html({defaultMessage: "Failed to load agents."}), xhr);
            render_list([]);
        },
    });
}

export function launch(memory_context?: LocationMemoryContext): void {
    $("#agents_overlay_container").html(render_agents_overlay());

    overlays.open_overlay({
        name: "agents",
        $overlay: $("#agents_overlay"),
        on_close() {
            overlay_is_open = false;
            current_memory_blocks_agent_id = null;
            browser_history.exit_overlay();
        },
    });

    overlay_is_open = true;
    clear_error();
    load_agents();

    // Load location memory if context provided, otherwise default to org memory
    const context = memory_context ?? {scope: "org"};
    const memory_html = location_memory_ui.render_section(context);
    $("#location-memory-container").html(memory_html);
    location_memory_ui.load_memory(context);

    // Best-effort focus.
    if (memory_context) {
        // If launched with memory context, focus on the textarea
        setTimeout(() => {
            $("#location-memory-textarea").trigger("focus");
        }, 100);
    } else {
        $("#create-agent-name").trigger("focus");
    }
}

function post_with_fallback(
    urls: string[],
    data: Record<string, string | number>,
    on_success: () => void,
    on_error: (xhr: JQuery.jqXHR<unknown>) => void,
): void {
    const [url, ...fallback_urls] = urls;
    if (!url) {
        return;
    }

    void channel.post({
        url,
        data,
        success() {
            on_success();
        },
        error(xhr) {
            if (xhr.status === 404 && fallback_urls.length > 0) {
                post_with_fallback(fallback_urls, data, on_success, on_error);
                return;
            }
            on_error(xhr);
        },
    });
}

function archive_with_fallback(
    urls: string[],
    on_success: () => void,
    on_error: (xhr: JQuery.jqXHR<unknown>) => void,
): void {
    const [url, ...fallback_urls] = urls;
    if (!url) {
        return;
    }

    void channel.post({
        url,
        data: {},
        success() {
            on_success();
        },
        error(xhr) {
            if (xhr.status === 404 && fallback_urls.length > 0) {
                archive_with_fallback(fallback_urls, on_success, on_error);
                return;
            }
            on_error(xhr);
        },
    });
}

function get_memory_blocks_error_box(): JQuery {
    return $(".memory-blocks-panel .memory-blocks-error");
}

function clear_memory_blocks_error(): void {
    const $error = get_memory_blocks_error_box();
    $error.removeClass("show alert-error alert-success").empty();
}

function show_memory_blocks_client_error(message_html: string): void {
    ui_report.client_error(message_html, get_memory_blocks_error_box());
}

function show_memory_blocks_server_error(message_html: string, xhr: JQuery.jqXHR<unknown>): void {
    ui_report.error(message_html, xhr, get_memory_blocks_error_box());
}

function show_memory_blocks_success(message_html: string): void {
    ui_report.success(message_html, get_memory_blocks_error_box(), 1200);
}

function get_xhr_json_message(xhr: JQuery.jqXHR<unknown>): string | undefined {
    const response = xhr.responseJSON;
    if (!response || typeof response !== "object") {
        return undefined;
    }
    const msg = (response as Record<string, unknown>)["msg"];
    return typeof msg === "string" && msg.trim() !== "" ? msg : undefined;
}

function extract_memory_blocks(data: unknown): MemoryBlock[] {
    // Handle Zulip json_success nesting: accept either `{blocks:[...]}` or `{data:{blocks:[...]}}`.
    let blocks_array: unknown[] | undefined;

    if (data && typeof data === "object") {
        const data_obj = data as Record<string, unknown>;

        if (Array.isArray(data_obj["blocks"])) {
            blocks_array = data_obj["blocks"] as unknown[];
        } else if (data_obj["data"] && typeof data_obj["data"] === "object") {
            const nested_data = data_obj["data"] as Record<string, unknown>;
            if (Array.isArray(nested_data["blocks"])) {
                blocks_array = nested_data["blocks"] as unknown[];
            }
        }
    }

    if (blocks_array) {
        return blocks_array
            .filter((b) => b && typeof b === "object")
            .map((b) => {
                const block = b as Record<string, unknown>;
                const id = block["id"];
                const label = block["label"];
                const value = block["value"];
                return {
                    id: typeof id === "string" ? id : "",
                    label: typeof label === "string" ? label : "",
                    value: typeof value === "string" ? value : "",
                    ...block,
                };
            })
            .filter((b) => b.id !== "");
    }
    return [];
}

function render_memory_blocks(blocks: MemoryBlock[]): void {
    $(".memory-blocks-container").html(render_memory_blocks_list({blocks}));
}

function set_memory_blocks_loading_state(): void {
    $(".memory-blocks-container").html(
        `<div class="no-overlay-messages">${$t_html({defaultMessage: "Loading…"})}</div>`,
    );
}

function load_memory_blocks(agent_id: string): void {
    if (!overlay_is_open) {
        return;
    }

    clear_memory_blocks_error();
    set_memory_blocks_loading_state();

    void channel.get({
        url: `/json/smarty_pants/agents/${encodeURIComponent(agent_id)}/memory/blocks`,
        success(data) {
            render_memory_blocks(extract_memory_blocks(data));
        },
        error(xhr) {
            show_memory_blocks_server_error(
                $t_html({defaultMessage: "Failed to load memory blocks."}),
                xhr,
            );
            render_memory_blocks([]);
        },
    });
}

function show_memory_blocks_panel(agent_id: string, agent_name: string): void {
    current_memory_blocks_agent_id = agent_id;

    const panel_html = render_memory_blocks_panel({
        agentId: agent_id,
        agentName: agent_name,
    });

    // Insert the panel into the overlay (below the agents list)
    const $existing_panel = $(".memory-blocks-panel");
    if ($existing_panel.length > 0) {
        $existing_panel.replaceWith(panel_html);
    } else {
        $("#agents-list").after(panel_html);
    }

    load_memory_blocks(agent_id);
}

function hide_memory_blocks_panel(): void {
    current_memory_blocks_agent_id = null;
    $(".memory-blocks-panel").remove();
}

export function initialize(): void {
    location_memory_ui.initialize();

    $("body").on("click", "#agents_overlay .edit-budget-agent-button", (e) => {
        e.preventDefault();
        if (!overlay_is_open) {
            return;
        }

        const $agent_row = $(e.currentTarget).closest(".agent-row");
        const $form = $agent_row.find(".agent-budget-inline-form");
        if ($form.length === 0) {
            return;
        }

        $form.toggle();
    });

    $("body").on("submit", "#agents_overlay .agent-budget-inline-form", (e) => {
        e.preventDefault();
        if (!overlay_is_open) {
            return;
        }

        const $form = $(e.currentTarget);
        const $agent_row = $form.closest(".agent-row");
        const agent_id = $agent_row.attr("data-agent-id");
        if (!agent_id) {
            show_client_error($t_html({defaultMessage: "Missing agent id."}));
            return;
        }

        const $save_button = $form.find(".save-budget-agent-button");
        $save_button.prop("disabled", true);

        const monthly_usd = ($form.find(".agent-budget-monthly-usd").val() as string).trim();
        const daily_runs = ($form.find(".agent-budget-daily-runs").val() as string).trim();

        clear_error();

        void channel.patch({
            url: `/json/smarty_pants/agents/${encodeURIComponent(agent_id)}/budget`,
            data: {
                budgetMonthlyUsd: monthly_usd === "" ? "" : monthly_usd,
                budgetDailyRuns: daily_runs === "" ? "" : daily_runs,
            },
            success() {
                show_success($t_html({defaultMessage: "Budget updated."}));
                load_agents();
                $save_button.prop("disabled", false);
            },
            error(xhr) {
                show_server_error_prefer_json_msg(
                    $t_html({defaultMessage: "Failed to update budget."}),
                    xhr,
                );
                $save_button.prop("disabled", false);
            },
        });
    });

    $("body").on("submit", "#create-agent-form", (e) => {
        e.preventDefault();
        if (!overlay_is_open) {
            return;
        }

        const $form = $(e.currentTarget);
        const $submit_button = $form.find("button[type='submit'], input[type='submit']").first();
        $submit_button.prop("disabled", true);

        const $input = $("#create-agent-name");
        const name = ($input.val() as string).trim();
        if (!name) {
            show_client_error($t_html({defaultMessage: "Agent name is required."}));
            $submit_button.prop("disabled", false);
            return;
        }

        clear_error();

        post_with_fallback(
            ["/json/smarty_pants/agents"],
            {name},
            () => {
                $input.val("");
                show_success($t_html({defaultMessage: "Agent created."}));
                load_agents();
                $submit_button.prop("disabled", false);
            },
            (xhr) => {
                show_server_error_prefer_json_msg(
                    $t_html({defaultMessage: "Failed to create agent."}),
                    xhr,
                );
                $submit_button.prop("disabled", false);
            },
        );
    });

    $("body").on("submit", "#attach-agent-form", (e) => {
        e.preventDefault();
        if (!overlay_is_open) {
            return;
        }

        const $form = $(e.currentTarget);
        const $submit_button = $form.find("button[type='submit'], input[type='submit']").first();
        $submit_button.prop("disabled", true);

        const $runtime_agent_id_input = $("#attach-runtime-agent-id");
        const runtime_agent_id = ($runtime_agent_id_input.val() as string).trim();
        if (!runtime_agent_id) {
            show_client_error($t_html({defaultMessage: "Runtime agent id is required."}));
            $submit_button.prop("disabled", false);
            return;
        }

        const $name_input = $("#attach-agent-name");
        const name = ($name_input.val() as string).trim();

        const data: Record<string, string | number> = {runtime_agent_id};
        if (name) {
            data["name"] = name;
        }

        clear_error();

        post_with_fallback(
            ["/json/smarty_pants/agents/attach"],
            data,
            () => {
                $runtime_agent_id_input.val("");
                $name_input.val("");
                show_success($t_html({defaultMessage: "Agent attached."}));
                load_agents();
                $submit_button.prop("disabled", false);
            },
            (xhr) => {
                show_server_error_prefer_json_msg(
                    $t_html({defaultMessage: "Failed to attach agent."}),
                    xhr,
                );
                $submit_button.prop("disabled", false);
            },
        );
    });

    $("body").on("click", "#agents_overlay .archive-agent-button", (e) => {
        e.preventDefault();
        if (!overlay_is_open) {
            return;
        }

        const agent_id = $(e.currentTarget).closest(".agent-row").attr("data-agent-id");
        if (!agent_id) {
            show_client_error($t_html({defaultMessage: "Missing agent id."}));
            return;
        }

        clear_error();

        archive_with_fallback(
            [`/json/smarty_pants/agents/${encodeURIComponent(agent_id)}/archive`],
            () => {
                show_success($t_html({defaultMessage: "Agent archived."}));
                load_agents();
            },
            (xhr) => {
                show_server_error_prefer_json_msg(
                    $t_html({defaultMessage: "Failed to archive agent."}),
                    xhr,
                );
            },
        );
    });

    $("body").on("click", "#agents_overlay .toggle-pause-agent-button", (e) => {
        e.preventDefault();
        if (!overlay_is_open) {
            return;
        }

        const $button = $(e.currentTarget);
        $button.prop("disabled", true);

        const $agent_row = $(e.currentTarget).closest(".agent-row");
        const agent_id = $agent_row.attr("data-agent-id");
        if (!agent_id) {
            show_client_error($t_html({defaultMessage: "Missing agent id."}));
            return;
        }

        const paused_attr = ($agent_row.attr("data-paused") ?? "false").toLowerCase();
        const is_paused = paused_attr === "true";
        const desired_paused_state = !is_paused;

        clear_error();

        void channel.post({
            url: `/json/smarty_pants/agents/${encodeURIComponent(agent_id)}/pause`,
            data: {paused: desired_paused_state},
            success() {
                if (desired_paused_state) {
                    show_success($t_html({defaultMessage: "Agent paused."}));
                } else {
                    show_success($t_html({defaultMessage: "Agent unpaused."}));
                }
                load_agents();
                $button.prop("disabled", false);
            },
            error(xhr) {
                show_server_error_prefer_json_msg(
                    $t_html({defaultMessage: "Failed to update paused state."}),
                    xhr,
                );
                $button.prop("disabled", false);
            },
        });
    });

    // Memory blocks management
    $("body").on("click", "#agents_overlay .manage-memory-blocks-button", (e) => {
        e.preventDefault();
        if (!overlay_is_open) {
            return;
        }

        const $agent_row = $(e.currentTarget).closest(".agent-row");
        const agent_id = $agent_row.attr("data-agent-id");
        const agent_name = $agent_row.find(".agent-name").text().trim();

        if (!agent_id) {
            show_client_error($t_html({defaultMessage: "Missing agent id."}));
            return;
        }

        show_memory_blocks_panel(agent_id, agent_name);
    });

    $("body").on("click", ".close-memory-blocks-button", (e) => {
        e.preventDefault();
        hide_memory_blocks_panel();
    });

    $("body").on("submit", ".memory-block-create-form", (e) => {
        e.preventDefault();
        if (!overlay_is_open || !current_memory_blocks_agent_id) {
            return;
        }

        const $form = $(e.currentTarget);
        const $submit_button = $form.find("button[type='submit'], input[type='submit']").first();
        $submit_button.prop("disabled", true);
        const $label_input = $form.find(".memory-block-label-input");
        const $description_input = $form.find(".memory-block-description-input");
        const $value_input = $form.find(".memory-block-value-input");

        const label = ($label_input.val() as string).trim();
        const value = $value_input.val() as string;
        const description = ($description_input.val() as string | undefined)?.trim() ?? "";

        if (!label) {
            show_memory_blocks_client_error($t_html({defaultMessage: "Label is required."}));
            $submit_button.prop("disabled", false);
            return;
        }

        clear_memory_blocks_error();

        const data: Record<string, string> = {label, value};
        if (description !== "") {
            data["description"] = description;
        }

        void channel.post({
            url: `/json/smarty_pants/agents/${encodeURIComponent(current_memory_blocks_agent_id)}/memory/blocks`,
            data,
            success() {
                $label_input.val("");
                $description_input.val("");
                $value_input.val("");
                load_memory_blocks(current_memory_blocks_agent_id!);
                show_memory_blocks_success($t_html({defaultMessage: "Memory block created."}));
                $submit_button.prop("disabled", false);
            },
            error(xhr) {
                // Convex may return `{error: "label_conflict", message: "..."}` which Zulip
                // surfaces as a JsonableError with `msg` set to the server message.
                // Prefer showing that message directly for a clearer UX.
                const server_msg = get_xhr_json_message(xhr);
                if (server_msg !== undefined) {
                    show_memory_blocks_client_error(escape_text_for_html(server_msg));
                    $submit_button.prop("disabled", false);
                    return;
                }

                show_memory_blocks_server_error(
                    $t_html({defaultMessage: "Failed to create memory block."}),
                    xhr,
                );
                $submit_button.prop("disabled", false);
            },
        });
    });

    $("body").on("click", ".save-memory-block-button", (e) => {
        e.preventDefault();
        if (!overlay_is_open || !current_memory_blocks_agent_id) {
            return;
        }

        const $button = $(e.currentTarget);
        // Prevent duplicate clicks while the request is in-flight.
        $button.prop("disabled", true);

        const $block = $(e.currentTarget).closest(".memory-block");
        const block_id = $block.attr("data-block-id");
        const value = $block.find(".memory-block-value").val() as string;

        if (!block_id) {
            show_memory_blocks_client_error($t_html({defaultMessage: "Missing block id."}));
            $button.prop("disabled", false);
            return;
        }

        clear_memory_blocks_error();

        const data: Record<string, string> = {value};

        // Only send description if the UI actually collects it.
        const $description = $block.find(".memory-block-description");
        if ($description.length > 0) {
            const description = ($description.val() as string).trim();
            if (description !== "") {
                data["description"] = description;
            }
        }

        void channel.patch({
            url: `/json/smarty_pants/agents/${encodeURIComponent(current_memory_blocks_agent_id)}/memory/blocks/${encodeURIComponent(block_id)}`,
            data,
            success() {
                show_memory_blocks_success($t_html({defaultMessage: "Memory block saved."}));
                // Keep disabled until the list reload completes (best-effort).
                load_memory_blocks(current_memory_blocks_agent_id!);
            },
            error(xhr) {
                show_memory_blocks_server_error(
                    $t_html({defaultMessage: "Failed to save memory block."}),
                    xhr,
                );
                $button.prop("disabled", false);
            },
        });
    });

    $("body").on("click", ".delete-memory-block-button", (e) => {
        e.preventDefault();
        if (!overlay_is_open || !current_memory_blocks_agent_id) {
            return;
        }

        const $button = $(e.currentTarget);
        // Prevent duplicate clicks while the request is in-flight.
        $button.prop("disabled", true);

        const $block = $(e.currentTarget).closest(".memory-block");
        const block_id = $block.attr("data-block-id");

        if (!block_id) {
            show_memory_blocks_client_error($t_html({defaultMessage: "Missing block id."}));
            $button.prop("disabled", false);
            return;
        }

        clear_memory_blocks_error();

        void channel.del({
            url: `/json/smarty_pants/agents/${encodeURIComponent(current_memory_blocks_agent_id)}/memory/blocks/${encodeURIComponent(block_id)}`,
            success() {
                load_memory_blocks(current_memory_blocks_agent_id!);
                show_memory_blocks_success($t_html({defaultMessage: "Memory block deleted."}));
                // Keep disabled; the button will disappear when the list reloads.
            },
            error(xhr) {
                show_memory_blocks_server_error(
                    $t_html({defaultMessage: "Failed to delete memory block."}),
                    xhr,
                );
                $button.prop("disabled", false);
            },
        });
    });
}
