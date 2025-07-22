// Add this to the scripts block in create_vm.html, after the existing JavaScript

// Form validation
document.getElementById('createVmForm').addEventListener('submit', function(event) {
    const vmType = typeSelect.value;
    const creationMethod = creationMethodSelect.value;
    const node = nodeSelect.value;
    const storage = storageSelect.value;
    const bridge = bridgeSelect.value;
    
    let isValid = true;
    let errorMessage = '';
    
    // Check if node is selected
    if (!node) {
        isValid = false;
        errorMessage = 'Please select a node';
    }
    
    // Check if storage is selected
    else if (!storage) {
        isValid = false;
        errorMessage = 'Please select a storage';
    }
    
    // Check if network bridge is selected
    else if (!bridge) {
        isValid = false;
        errorMessage = 'Please select a network bridge';
    }
    
    // For template-based creation, check if template is selected
    else if (creationMethod === 'template') {
        if (vmType === 'qemu' && !templateSelect.value) {
            isValid = false;
            errorMessage = 'Please select a VM template';
        }
    }
    
    // For LXC, check if OS template is selected
    else if (vmType === 'lxc' && !osTemplateSelect.value) {
        isValid = false;
        errorMessage = 'Please select an OS template for the container';
    }
    
    // If validation fails, prevent form submission and show error
    if (!isValid) {
        event.preventDefault();
        alert(errorMessage);
    }
});


///////////////////////

// Add this to the scripts block in create_vm.html, after the existing JavaScript

// Loading indicator functions
function showLoading(element) {
    element.disabled = true;
    element.innerHTML = '<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span> Loading...';
}

function hideLoading(element, originalText) {
    element.disabled = false;
    element.innerHTML = originalText;
}

// Add loading indicator to form submission
document.getElementById('createVmForm').addEventListener('submit', function(event) {
    // Only show loading if validation passed
    if (this.checkValidity()) {
        const submitButton = this.querySelector('button[type="submit"]');
        showLoading(submitButton);
    }
});

// Add loading indicators to dropdowns
nodeSelect.addEventListener('change', function() {
    if (this.value) {
        storageSelect.innerHTML = '<option>Loading storages...</option>';
        bridgeSelect.innerHTML = '<option>Loading networks...</option>';
        if (creationMethodSelect.value === 'template') {
            templateSelect.innerHTML = '<option>Loading templates...</option>';
        }
        if (typeSelect.value === 'lxc') {
            osTemplateSelect.innerHTML = '<option>Loading templates...</option>';
        }
    }
});