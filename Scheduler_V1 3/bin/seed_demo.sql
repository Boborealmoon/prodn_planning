INSERT OR IGNORE INTO parts (part_id, part_name, part_desc) VALUES
(1, 'SHAFT-A100', 'Demo shaft with turning and grinding'),
(2, 'BRACKET-B200', 'Milled bracket'),
(3, 'HOUSING-C300', 'Bored housing'),
(4, 'PLATE-D400', 'Simple plate');

INSERT OR IGNORE INTO part_flow_header (flow_id, part_id, flow_code, flow_name, is_default) VALUES
(1, 1, 'FLOW-A', 'Standard shaft route', 1),
(2, 1, 'FLOW-A-ALT', 'Alternate shaft route', 0),
(3, 2, 'FLOW-B', 'Bracket route', 1),
(4, 3, 'FLOW-C', 'Housing route', 1),
(5, 4, 'FLOW-D', 'Plate route', 1);

INSERT OR IGNORE INTO operation_seq
(step_id, flow_id, seq_no, op_no, op_type, machine_category, cycle_time, setup_time, preferred_machine, is_last_op) VALUES
(1, 1, 10, 'TN10', 'Turning', 'LATHE', 4.0, 30, 'L001', 0),
(2, 1, 20, 'GR20', 'Grinding', 'GRINDER', 2.5, 20, 'G001', 1),
(3, 2, 10, 'TN10', 'Turning', 'LATHE', 3.8, 30, 'L002', 0),
(4, 2, 20, 'HT20', 'Heat Treat', 'FURNACE', 8.0, 15, 'HT01', 0),
(5, 2, 30, 'GR30', 'Grinding', 'GRINDER', 2.0, 20, 'G001', 1),
(6, 3, 10, 'ML10', 'Milling', 'MILL', 5.0, 40, 'M001', 1),
(7, 4, 10, 'BR10', 'Boring', 'BORING', 7.0, 45, 'B001', 1),
(8, 5, 10, 'ML10', 'Milling', 'MILL', 3.5, 25, 'M002', 1);

INSERT OR IGNORE INTO machines (machine_id, machine_code, machine_category, shift_profile, active, notes) VALUES
(1, 'L001', 'LATHE', 'STANDARD', 1, 'Main lathe'),
(2, 'L002', 'LATHE', 'STANDARD', 1, 'Backup lathe'),
(3, 'M001', 'MILL', 'STANDARD', 1, ''),
(4, 'M002', 'MILL', 'STANDARD', 1, ''),
(5, 'G001', 'GRINDER', 'STANDARD', 1, ''),
(6, 'B001', 'BORING', 'STANDARD', 1, ''),
(7, 'P001', 'POLISH', 'STANDARD', 1, ''),
(8, 'HT01', 'FURNACE', '24HR', 1, 'Can continue overnight');

INSERT OR IGNORE INTO process_sheet
(ps_id, part_id, part_no, inv_desc, order_date, due_date, total_qty, selected_flow_id, planner_status, status) VALUES
('PS-1001', 1, 'INV-SHAFT-A100', 'Demo shaft batch', date('now','-2 day'), date('now','+7 day'), 60, 1, 'UNPLANNED', 'ACTIVE'),
('PS-1002', 2, 'INV-BRACKET-B200', 'Bracket rush order', date('now','-1 day'), date('now','+3 day'), 35, 3, 'UNPLANNED', 'ACTIVE'),
('PS-1003', 3, 'INV-HOUSING-C300', 'Housing batch', date('now'), date('now','+14 day'), 20, 4, 'UNPLANNED', 'ACTIVE'),
('PS-1004', 4, 'INV-PLATE-D400', 'Plate order', date('now'), date('now','+10 day'), 80, 5, 'UNPLANNED', 'ON_HOLD'),
('PS-1005', 1, 'INV-SHAFT-A100-ALT', 'Alternate shaft route', date('now','-10 day'), date('now','-1 day'), 25, 2, 'UNPLANNED', 'ACTIVE');

INSERT OR IGNORE INTO process_sheet_material
(ps_id, material_name, material_ready, material_ready_qty, need_by_date, order_status, planner_note) VALUES
('PS-1001', 'Steel bar 25mm', 1, 60, '', 'NA', ''),
('PS-1002', 'Aluminum block', 0, 10, date('now','+1 day'), 'ORDERED', 'Partial stock only'),
('PS-1003', 'Cast housing blank', 1, 20, '', 'NA', ''),
('PS-1004', 'Plate stock', 0, 0, date('now','+4 day'), 'TO_ORDER', ''),
('PS-1005', 'Steel bar 25mm', 1, 25, '', 'NA', '');

INSERT OR IGNORE INTO process_sheet_material_order_log
(mat_id, ps_id, ordered_qty, received_qty, order_date, expected_date, log_status, note)
SELECT mat_id, ps_id, 25, 0, date('now','-1 day'), date('now','+1 day'), 'PENDING', 'Demo PO'
FROM process_sheet_material WHERE ps_id = 'PS-1002';

INSERT OR IGNORE INTO staff (staff_id, staff_name, role, active) VALUES
(1, 'Alex Tan', 'MACHINIST', 1),
(2, 'Jamie Lee', 'MACHINIST', 1),
(3, 'Morgan Lim', 'OPERATOR', 1),
(4, 'Priya Koh', 'OPERATOR', 1),
(5, 'Sam Wong', 'MACHINIST', 1);
