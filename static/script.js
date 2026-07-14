(function() {
    const themeKey = 'proxui-theme';

    function readSavedTheme() {
        try {
            return localStorage.getItem(themeKey);
        } catch (error) {
            return null;
        }
    }

    function saveTheme(theme) {
        try {
            localStorage.setItem(themeKey, theme);
        } catch (error) {
            // Ignore storage failures; the active document still gets themed.
        }
    }

    function getPreferredTheme() {
        const savedTheme = readSavedTheme();
        if (savedTheme === 'dark' || savedTheme === 'light') {
            return savedTheme;
        }
        return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }

    function updateThemeToggle(theme) {
        const button = document.getElementById('themeToggle');
        const icon = document.getElementById('themeToggleIcon');
        if (!button || !icon) {
            return;
        }

        const isDark = theme === 'dark';
        icon.className = isDark ? 'bi bi-sun' : 'bi bi-moon-stars';
        button.setAttribute('aria-label', isDark ? 'Switch to light mode' : 'Switch to dark mode');
        button.setAttribute('title', isDark ? 'Switch to light mode' : 'Switch to dark mode');
    }

    function applyTheme(theme, persist = true) {
        document.documentElement.setAttribute('data-theme', theme);
        document.documentElement.setAttribute('data-bs-theme', theme);
        if (persist) {
            saveTheme(theme);
        }
        updateThemeToggle(theme);
    }

    window.ProxUITheme = {
        current: function() {
            return document.documentElement.getAttribute('data-theme') || getPreferredTheme();
        },
        toggle: function() {
            const nextTheme = this.current() === 'dark' ? 'light' : 'dark';
            applyTheme(nextTheme);
        },
        set: applyTheme
    };

    document.addEventListener('DOMContentLoaded', function() {
        const theme = getPreferredTheme();
        applyTheme(theme, false);
        // Theme toggle click is handled by Alpine x-data @click="window.ProxUITheme.toggle()" in base.html
    });
})();
