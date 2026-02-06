
const assert = require("node:assert/strict");

const {run_test} = require("./lib/test.cjs");

// Compile .hbs templates in strict mode (same behavior as production).
require("./lib/handlebars.cjs").hook_require();

const render_agents_list = require("../templates/agents_list.hbs");

run_test("agents_list.hbs tolerates missing optional budget fields", () => {
    const agents = [
        {
            id: "k1",
            name: "Demo Agent",
            usage: {
                runsToday: 0,
                costUsd30d: 0,
                costUsdMonth: 0,
            },
            // Intentionally omit budgetMonthlyUsd / budgetDailyRuns.
            // This used to crash under strict handlebars.
        },
    ];

    assert.doesNotThrow(() => {
        const html = render_agents_list({agents});
        assert.ok(typeof html === "string");
        assert.ok(html.includes("Demo Agent"));
    });
});
