// Make a string html-safe.
function safe(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;")
            .replace(/>/g, "&gt;").replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
}

// Javascript to hide all nodes (rows) that are children of a given node.
// Each node is given a unique integer id when created; that should be
// passed in here.
function toggleCollapse(id) {
    [...document.getElementsByClassName(`childOf${id}`)].forEach(
        node => node.classList.toggle(`collapse${id}`)
    );
}

// Given a collection of builds, each of which has a node-tree, return
// a linear list of nodes that includes every node in every build.
// This is necessary because each node is a row of our graph, and of
// course html tables render rows in linear order.  We still keep
// track of the tree structure via two new fields we add to each node:
// parentIDs and hasChildren.
function getNodeList(builds) {
    var nodes = [];

    // Handle one node-tree.
    function flattenNodes(node, parentIDs) {
       var myID = nodes.length;  // any unique integer will do
       nodes = [
           ...nodes,
           {...node, id: myID, parentIDs, hasChildren: !!node.children.length}
       ];
       node.children.forEach(c => flattenNodes(c, [...parentIDs, myID]));
    }

    builds.forEach(build => flattenNodes(build.nodeRoot, []));
    return nodes;
}

// Insert the CSS for the grid marks.  We need to do this dynamically
// because we want them every 60 seconds, and we need to know he
// width of the graph (in seconds) to do that.
function addCssGridMarks(deployTimeMs) {
    var tickIntervalMs = 60 * 1000;
    var numTicks = deployTimeMs / tickIntervalMs;
    // Let's make sure we don't have too many ticks.  20 seems a good maximum.
    while (numTicks > 20) {
       tickIntervalMs += 60 * 1000;
       var numTicks = deployTimeMs / tickIntervalMs;
    }
    var tickGapPct = 100.0 / numTicks;
    document.styleSheets[0].insertRule(`.bar-container {
      background-size: ${tickGapPct}% 100%;
      background-image: linear-gradient(to right, #BBBBBB 1px, transparent 1px);
    }`);
    // This ensures that the numbers on the x-axis have the right spacing.
    document.styleSheets[0].insertRule(`.axis-label {
      max-width: ${tickGapPct}%;
    }`);
    // This is needed for the very first number.
    document.styleSheets[0].insertRule(`.pre-axis-label {
      max-width: ${tickGapPct/2}%;
    }`);
    return {numTicks, tickIntervalMs};
}

// TODO(csilvers): hard-code this in the css instead.
function addCssColors(colors) {
    // Insert the CSS for the colors.  The white color we actually
    // want to be transparent (so grid-marks show up on it), so we
    // handle that case specially.  In addition there's a style to be
    // *explicitly* transparent.
    colors.forEach((c, i) => {
        if (c.match(/#ffffff/i)) {
            document.styleSheets[0].insertRule(`.c${i} { visibility: hidden; }`)
        } else {
            document.styleSheets[0].insertRule(`.c${i} { background: ${c}; }`)
        }
    });
    document.styleSheets[0].insertRule(`.ctransparent { visibility: hidden; }`);
}


// TODO(csilvers): document what's in `data`
function renderChart(data) {
    var deployTimeMs = data.deployEndTimeMs - data.deployStartTimeMs;

    // Add some CSS we need that must be generated dynamically.
    addCssColors(data.colors);
    var {numTicks, tickIntervalMs} = addCssGridMarks(deployTimeMs);

    // We want each bar to look like this:
    //   <div class="tr">
    //     <div class="label">e2e-worker-1</div>
    //     <div class="bar-container">
    //       <div class="bar">
    //         <div class="interval c1" style="max-width:10.5%">
    //           <div class="tooltip">WAITING: 0 - 6.5</div>
    //         </div>
    //         <div class="interval c2" style="max-width:89.5%">
    //           <div class="tooltip">RUNNING: 6.5 - 60</div>
    //         </div>
    //       </div>
    //     </div>
    //   </div>
    // We create all this html in one go and use innerHTML to insert it
    // into the html proper.
    var html = [];
    var nodes = getNodeList(data.jobs);
    nodes.forEach(node => {
        var id = node.id;
        // Add a style that children can use to collapse this node
        document.styleSheets[0].insertRule(`.collapse${id} {visibility:collapse}`);

        // Add some unstyled classes that we can use to look up an
        // entire subtree via document.getElementsByClassName("childOfX").
        html.push(`<div class="tr ${node.parentIDs.map(i => `childOf${i}`).join(" ")}">`);

       // Add the bar's label, with a "collapse triangle" if appropriate.
       var indent = node.parentIDs.length + 1;
        if (node.hasChildren) {
            html.push(`<input id="collapsible${id}" class="toggle" ` +
                      `type="checkbox" ` +
                      `onclick="javascript:toggleCollapse(${id})">`);
            html.push(`<label for="collapsible${id}" class="lbl-toggle">`);
        }
        html.push(`<div class="label" style="padding-left: ${indent}em">`);
        html.push(`${safe(node.name)}`);
        html.push(`</div>`);
        if (node.hasChildren) {
            html.push(`</label>`);
        }

        // Add the bar!
        html.push(`<div class="bar-container">`);
        html.push(`<div class="bar">`);
        // So all our jobs line up on the x-axis, we insert "fake"
        // intervals from deploy-start-time to job-start-time,
        // and from job-end-time to deploy-end-time.
        var preJobInterval = {
            startTimeMs: data.deployStartTimeMs,
            endTimeMs: node.intervals[0].startTimeMs,
            timeRangeRelativeToJobStart: "",
            mode: "[job not started]",
            colorIndex: "transparent",
        };
        var intervals = [preJobInterval, ...node.intervals];
        intervals.forEach(interval => {
            var pct = ((interval.endTimeMs - interval.startTimeMs) * 100
                       / deployTimeMs);
            html.push(`<div class="interval c${interval.colorIndex}" ` +
                      `style="max-width:${pct}%">`);
            html.push(`<div class="tooltip">${safe(interval.mode)}: ` +
                      `${interval.timeRangeRelativeToJobStart}</div>`);
            html.push(`</div>`);
        });
        html.push(`</div>`);
        html.push(`</div>`);
        html.push(`</div>`);
    });

    // And insert a row of html to serve as the x-axis.  We center
    // each number under the grid by putting it in a span that is
    // centered under the grid-mark.  This doesn't necessarily work
    // for the last grid-mark, which might not have enough space after
    // it for the span, so we leave that as a TODO.
    // First, let's continue the grid-marks down below the graph a teeny bit.
    html.push(`<div class="tr">`);
    html.push(`<div class="label lastrow"></div>`);
    html.push(`<div class="bar-container lastrow"></div>`);
    html.push(`</div>`);

    html.push(`<div class="tr">`);
    html.push(`<div class="label"></div>`);
    html.push(`<div class="x-axis">`);
    html.push(`<div class="pre-axis-label"></div>`);
    // TODO(csilvers): handle last tick.
    for (var i = 1; i < numTicks - 1; i++) {
        html.push(`<div class="axis-label">`);
        html.push(`${Math.round(i * tickIntervalMs / 60000)}m`);
        html.push(`</div>`);
    }
    html.push(`</div>`);
    html.push(`</div>`);

    // Finally insert the html into the page!
    document.getElementById('title').innerHTML = safe(data.title);
    document.getElementById('perfchart').innerHTML = html.join("\n");
}
