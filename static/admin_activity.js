// Admin Activity Logs Tab - filtering, pagination

document.addEventListener('DOMContentLoaded', function() {
    const filterForm = document.getElementById('activityFilterForm');
    const logsTable = document.getElementById('logsTable');
    const filterGroup = document.getElementById('filterGroup');
    const filterAction = document.getElementById('filterAction');
    const filterLimit = document.getElementById('filterLimit');

    if (!filterForm || !logsTable || !filterGroup || !filterAction || !filterLimit) {
        return;
    }

    let debounceTimer;

    function escapeHtml(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function formatDetails(log) {
        const summary = log.summary || '';
        const details = log.details || '';
        if (summary && details && summary !== details) {
            return `<div>${escapeHtml(summary)}</div><div class="text-muted">${escapeHtml(details)}</div>`;
        }
        return escapeHtml(details || summary || '-');
    }

    function renderLogs(logs) {
        const tbody = logsTable.querySelector('tbody');
        if (!tbody) {
            return;
        }
        tbody.innerHTML = '';

        if (!Array.isArray(logs) || logs.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" class="text-muted">No logs found</td></tr>';
            return;
        }

        logs.forEach(log => {
            const row = document.createElement('tr');
            row.innerHTML = `
                <td><small>${escapeHtml(log.timestamp || '')}</small></td>
                <td>${escapeHtml(log.user_email || log.user_id || 'system')}</td>
                <td>${escapeHtml(log.group || '-')}</td>
                <td><code>${escapeHtml(log.action || '-')}</code></td>
                <td><small>${formatDetails(log)}</small></td>
            `;
            tbody.appendChild(row);
        });
    }

    function performSearch() {
        const group = filterGroup.value || '';
        const action = filterAction.value || '';
        const limit = filterLimit.value || 100;
        const url = `/api/admin/activity-logs?group=${encodeURIComponent(group)}&action=${encodeURIComponent(action)}&limit=${encodeURIComponent(limit)}`;

        fetch(url)
            .then(resp => resp.json())
            .then(data => {
                const logs = Array.isArray(data)
                    ? data
                    : Array.isArray(data?.logs)
                        ? data.logs
                        : Array.isArray(data?.data)
                            ? data.data
                            : [];
                renderLogs(logs);
            })
            .catch(err => {
                console.error('Failed to fetch activity logs', err);
                if (typeof uiAlert === 'function') {
                    uiAlert('Failed to fetch logs');
                }
            });
    }

    filterForm.addEventListener('submit', function(e) {
        e.preventDefault();
        performSearch();
    });

    [filterGroup, filterAction, filterLimit].forEach(input => {
        input.addEventListener('input', function() {
            clearTimeout(debounceTimer);
            debounceTimer = setTimeout(performSearch, 500);
        });
    });

    performSearch();
    setInterval(performSearch, 60000);
});
