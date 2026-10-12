// OABM-owned process boundary; the formulas and dimensional checks are upstream.
import { readFileSync } from 'node:fs';
import { voltageDrop, designCurrentSinglePhase } from './upstream/electrical/electrics.ts';
import { Quantity, dimension } from './upstream/units/quantity.ts';

const input = JSON.parse(readFileSync(0, 'utf8'));
const power = new Quantity(input.real_power_w, dimension({ M: 1, L: 2, T: -3 }));
const voltage = new Quantity(input.voltage_v, dimension({ M: 1, L: 2, T: -3, I: -1 }));
const length = new Quantity(input.one_way_length_m, dimension({ L: 1 }));
const current = designCurrentSinglePhase(power, voltage, input.power_factor);
const drop = voltageDrop(current, length, input.resistance_ohm_per_m, 'single');
process.stdout.write(JSON.stringify({
  current_a: current.si,
  voltage_drop_v: drop.si,
  voltage_drop_percent: 100 * drop.si / voltage.si,
}));
