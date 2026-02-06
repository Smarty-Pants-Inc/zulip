import $ from "jquery";

import render_location_memory_section from "../templates/location_memory_section.hbs";

import * as channel from "./channel.ts";
import {$t_html} from "./i18n.ts";
import * as ui_report from "./ui_report.ts";

export type LocationMemoryScope = "org" | "project" | "thread";

export type LocationMemoryContext = {
    scope: LocationMemoryScope;
    stream_id?: number;
    stream_name?: string;
    topic?: string;
};

let current_context: LocationMemoryContext | null = null;
let is_loading = false;
let is_saving = false;

function get_error_box(): JQuery {
    return $("#location_memory_error");
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
    ui_report.success(message_html, get_error_box());
}

function set_loading_state(): void {
    is_loading = true;
    $("#location-memory-textarea").prop("disabled", true).val("Loading…");
    $("#save-location-memory-button").prop("disabled", true);
}

function set_saving_state(): void {
    is_saving = true;
    $("#location-memory-textarea").prop("disabled", true);
    $("#save-location-memory-button").prop("disabled", true).text("Saving…");
}

function clear_loading_state(text: string): void {
    is_loading = false;
    $("#location-memory-textarea").prop("disabled", false).val(text);
    $("#save-location-memory-button").prop("disabled", false);
}

function clear_saving_state(): void {
    is_saving = false;
    $("#location-memory-textarea").prop("disabled", false);
    $("#save-location-memory-button").prop("disabled", false).text("Save");
}

function build_context_label(context: LocationMemoryContext): string {
    if (context.scope === "org") {
        return "Organization";
    }
    if (context.scope === "project" && context.stream_name) {
        return `Project: ${context.stream_name}`;
    }
    if (context.scope === "thread" && context.stream_name && context.topic) {
        return `Thread: ${context.stream_name} > ${context.topic}`;
    }
    return context.scope;
}

export function load_memory(context: LocationMemoryContext): void {
    current_context = context;
    clear_error();
    set_loading_state();

    const params: Record<string, string> = {
        scope: context.scope,
    };

    if (context.stream_id !== undefined) {
        params["stream_id"] = String(context.stream_id);
    }

    if (context.topic) {
        params["topic"] = context.topic;
    }

    void channel.get({
        url: "/json/smarty_pants/memory",
        data: params,
        success(data) {
            const text = typeof data.text === "string" ? data.text : "";
            clear_loading_state(text);
        },
        error(xhr) {
            show_server_error($t_html({defaultMessage: "Failed to load memory."}), xhr);
            clear_loading_state("");
        },
    });
}

export function save_memory(): void {
    if (!current_context) {
        show_client_error($t_html({defaultMessage: "No memory context selected."}));
        return;
    }

    if (is_saving) {
        return;
    }

    const text = ($("#location-memory-textarea").val() as string) || "";

    clear_error();
    set_saving_state();

    const data: Record<string, string | number> = {
        scope: current_context.scope,
        text,
    };

    if (current_context.stream_id !== undefined) {
        data["stream_id"] = current_context.stream_id;
    }

    if (current_context.topic) {
        data["topic"] = current_context.topic;
    }

    void channel.post({
        url: "/json/smarty_pants/memory",
        data,
        success() {
            clear_saving_state();
            show_success($t_html({defaultMessage: "Memory saved successfully."}));
            // Auto-dismiss success message after 2 seconds
            setTimeout(clear_error, 2000);
        },
        error(xhr) {
            clear_saving_state();
            show_server_error($t_html({defaultMessage: "Failed to save memory."}), xhr);
        },
    });
}

export function render_section(context: LocationMemoryContext): string {
    const context_label = build_context_label(context);
    return render_location_memory_section({
        context_label,
    });
}

export function initialize(): void {
    $("body").on("click", "#save-location-memory-button", (e) => {
        e.preventDefault();
        save_memory();
    });
}
