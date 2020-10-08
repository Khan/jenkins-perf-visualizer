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
function addCssGridMarks(taskTimeMs) {
    var tickIntervalMs = 60 * 1000;
    var numTicks = taskTimeMs / tickIntervalMs;
    // Let's make sure we don't have too many ticks.  20 seems a good maximum.
    while (numTicks > 20) {
       tickIntervalMs += 60 * 1000;
       var numTicks = taskTimeMs / tickIntervalMs;
    }
    var tickGapPct = 100.0 / numTicks;
    document.styleSheets[0].insertRule(`.bar-container {
      background-size: ${tickGapPct}% 100%;
      background-image: linear-gradient(to right, #BBBBBB 1px, transparent 1px);
    }`);
    // This ensures that the numbers on the x-axis have the right spacing.
    document.styleSheets[0].insertRule(`.axis-gap {
      max-width: ${tickGapPct}%;
    }`);
    return {numTicks, tickIntervalMs};
}

function addCssColors(colorToId) {
    // Insert the CSS for the colors.  The white color we actually
    // want to be transparent (so grid-marks show up on it), so we
    // handle that case specially.  In addition there's a style to be
    // *explicitly* transparent.
    Object.keys(colorToId).forEach(c => {
        var i = colorToId[c];
        if (c.match(/#ffffff/i)) {
            document.styleSheets[0].insertRule(`.c${i} { visibility: hidden; }`)
        } else {
            document.styleSheets[0].insertRule(`.c${i} { background: ${c}; }`)
        }
    });
    document.styleSheets[0].insertRule(`.ctransparent { visibility: hidden; }`);
}


// `data` has these fields:
//    title: the title of this graph.
//    subtitle: the subtitle of this graph (usually the timerange).
//    colorToId: a map from RRGGBB color-tuple to a unique id.
//        `builds`, below, refers to color using these unique id's.
//    taskStartTimeMs: when this graph started, as a time_t but in millisecs.
//    taskEndTimeMs: when this graph ended, as a time_t but in millisecs.
//    builds: a list of node-trees, one for each jenkins build in the graph.
function renderChart(data) {
    var taskTimeMs = data.taskEndTimeMs - data.taskStartTimeMs;

    // Add some CSS we need that must be generated dynamically.
    addCssColors(data.colorToId);
    var {numTicks, tickIntervalMs} = addCssGridMarks(taskTimeMs);

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
    var nodes = getNodeList(data.builds);
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
        // So all our builds line up on the x-axis, we insert "fake"
        // intervals from task-start-time to build-start-time,
        // and from build-end-time to task-end-time.
        var preBuildInterval = {
            startTimeMs: data.taskStartTimeMs,
            endTimeMs: node.intervals[0].startTimeMs,
            timeRangeRelativeToBuildStart: "",
            mode: "[build not started]",
            colorId: "transparent",
        };
        var intervals = [preBuildInterval, ...node.intervals];
        intervals.forEach(interval => {
            var pct = ((interval.endTimeMs - interval.startTimeMs) * 100
                       / taskTimeMs);
            html.push(`<div class="interval c${interval.colorId}" ` +
                      `style="max-width:${pct}%">`);
            html.push(`<div class="tooltip">${safe(interval.mode)}: ` +
                      `${interval.timeRangeRelativeToBuildStart}</div>`);
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
    for (var i = 0; i < numTicks; i++) {
        if (i > 0) {
            html.push(`<div class="axis-gap"></div>`);
        }
        html.push('<div class="axis-label">')
        html.push(`${Math.round(i * tickIntervalMs / 60000)}m`);
        html.push(`</div>`);
    }
    html.push(`</div>`);
    html.push(`</div>`);

    // Finally insert the html into the page!
    document.getElementById('title').innerHTML = safe(data.title);
    document.getElementById('subtitle').innerHTML = safe(data.subtitle);
    document.getElementById('perfchart').innerHTML = html.join("\n");
}
