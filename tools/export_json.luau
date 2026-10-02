-- SPDX-License-Identifier: MIT (script). Output data: ODbL 1.0, see LICENSE.
-- Converts data/*.luau into data/json/*.json. Run from the repository root: lune run tools/export_json
local fs = require("@lune/fs")
local serde = require("@lune/serde")
local NOTICE = "Contains information from OpenStreetMap, (c) OpenStreetMap contributors, made available under the Open Database License 1.0: https://opendatacommons.org/licenses/odbl/1-0/"
if not fs.isDir("data/json") then
	fs.writeDir("data/json")
end
for _, name in { "OsmBuildings", "OsmRoads", "OsmAreas" } do
	local data = require("../data/" .. name)
	local out = { license = "ODbL-1.0", notice = NOTICE, units = "studs (2.5 per metre); x east, y north; origin 57.180 N 65.641 E (equirectangular, see tools/osm_import.py project())", data = data }
	fs.writeFile("data/json/" .. name .. ".json", serde.encode("json", out, true))
	print("data/json/" .. name .. ".json")
end
