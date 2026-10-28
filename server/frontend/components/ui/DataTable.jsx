'use client';
import { useMemo, useState } from 'react';
import Link from 'next/link';
import ui from '../ui.module.css';

/*
 * columns: [{ key, label, render(row), sort(row) -> number|string, align }]
 * href(row) makes the first column a link to the row's own page.
 */
export default function DataTable({ columns, rows, rowKey, href, initialSort, empty = 'Nothing to show.', caption }) {
    const [sort, setSort] = useState(initialSort || { key: null, dir: 1 });
    const sorted = useMemo(() => {
        const column = columns.find(c => c.key === sort.key);
        if (!column?.sort) return rows;
        return [...rows].sort((a, b) => {
            const x = column.sort(a),
                y = column.sort(b);
            if (x == null) return 1;
            if (y == null) return -1;
            return (x < y ? -1 : x > y ? 1 : 0) * sort.dir;
        });
    }, [rows, columns, sort]);
    if (!rows.length) return <p className={ui.empty}>{empty}</p>;
    return (
        <div className={ui.tableWrap}>
            <table className={ui.table}>
                {caption && <caption className={ui.srOnly}>{caption}</caption>}
                <thead>
                    <tr>
                        {columns.map(c => (
                            <th
                                key={c.key}
                                scope="col"
                                style={{ textAlign: c.align || 'left' }}
                                aria-sort={sort.key === c.key ? (sort.dir > 0 ? 'ascending' : 'descending') : undefined}
                            >
                                {c.sort ? (
                                    <button
                                        type="button"
                                        className={ui.sortButton}
                                        onClick={() =>
                                            setSort(s => ({ key: c.key, dir: s.key === c.key ? -s.dir : 1 }))
                                        }
                                    >
                                        {c.label}
                                        <span aria-hidden="true">
                                            {sort.key === c.key ? (sort.dir > 0 ? ' ▲' : ' ▼') : ''}
                                        </span>
                                    </button>
                                ) : (
                                    c.label
                                )}
                            </th>
                        ))}
                    </tr>
                </thead>
                <tbody>
                    {sorted.map(row => (
                        <tr key={rowKey(row)}>
                            {columns.map((c, i) => (
                                <td key={c.key} style={{ textAlign: c.align || 'left' }}>
                                    {i === 0 && href ? (
                                        <Link className={ui.link} href={href(row)}>
                                            {c.render ? c.render(row) : row[c.key]}
                                        </Link>
                                    ) : c.render ? (
                                        c.render(row)
                                    ) : (
                                        row[c.key]
                                    )}
                                </td>
                            ))}
                        </tr>
                    ))}
                </tbody>
            </table>
        </div>
    );
}
