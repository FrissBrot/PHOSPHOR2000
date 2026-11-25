import boot from "./boot.js";
import dance from "./dance.js";
import spectrum from "./spectrum.js";
import grid from "./grid.js";
import rain from "./rain.js";
import countdown from "./countdown.js";
import mattliball from "./mattliball.js";
import guggen from "./guggen.js";
import stollen from "./stollen.js";
import mottos from "./mottos.js";
import bars from "./bars.js";
import hell from "./hell.js";
import calibration from "./calibration.js";

export const SCENES = { boot, dance, spectrum, grid, rain, countdown, mattliball, guggen, stollen, mottos, bars, hell, calibration };
export const PLAYABLE = Object.keys(SCENES).filter((s) => s !== "calibration");
