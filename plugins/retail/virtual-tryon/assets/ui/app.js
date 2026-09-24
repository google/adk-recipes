document.addEventListener("DOMContentLoaded", () => {
    // State management
    let activeCatalog = [];
    let selectedProduct = null;
    let userPhotoFile = null;
    let generatedImageBase64 = null;

    // DOM Elements
    const productsGrid = document.getElementById("products-grid");
    
    // Sidebar User Reference elements
    const uploadZone = document.getElementById("upload-zone");
    const userFileInput = document.getElementById("user-file-input");
    const userPhotoPreview = document.getElementById("user-photo-preview");
    const dropzonePromptContainer = document.getElementById("dropzone-prompt-container");
    const photoHoverOverlay = document.getElementById("photo-hover-overlay");
    const replacePhotoBtn = document.getElementById("replace-photo-btn");

    // Header elements
    const scanCatalogBtn = document.getElementById("scan-catalog-btn");
    const catalogPathInput = document.getElementById("catalog-path-input");
    const catalogPathDisplay = document.getElementById("catalog-path-display");
    const categoryFilterPills = document.querySelectorAll(".filter-pill");

    // Fitting Room panel elements
    const productSelectedCard = document.getElementById("product-selected-card");
    const productEmptyCard = document.getElementById("product-empty-card");
    const resultProductImg = document.getElementById("result-product-img");
    const clearProductSelection = document.getElementById("clear-product-selection");

    // Output elements
    const outputBadge = document.getElementById("output-badge");
    const tryonLoading = document.getElementById("tryon-loading");
    const tryonOutputImg = document.getElementById("tryon-output-img");
    const videoWrapper = document.getElementById("video-wrapper");
    const tryonOutputVideo = document.getElementById("tryon-output-video");
    const outputEmptyPlaceholder = document.getElementById("output-empty-placeholder");
    const loadingText = document.getElementById("loading-text");
    const configModel = document.getElementById("config-model");
    const laserScanner = document.getElementById("laser-scanner");

    // Action buttons
    const runTryonBtn = document.getElementById("run-tryon-btn");
    const runVideoBtn = document.getElementById("run-video-btn");
    const vtoStepsIndicator = document.getElementById("vto-steps-indicator");

    // Settings Panel elements
    const toggleSettingsBtn = document.getElementById("toggle-settings-btn");
    const closeSettingsBtn = document.getElementById("close-settings-btn");
    const settingsPanel = document.getElementById("settings-panel");

    // Initialize Page
    fetchCatalog();
    setupDropzone();
    setupPills();
    updateStatusIndicator();

    if (toggleSettingsBtn && settingsPanel) {
        toggleSettingsBtn.addEventListener("click", () => {
            settingsPanel.classList.toggle("hidden");
        });
    }
    if (closeSettingsBtn && settingsPanel) {
        closeSettingsBtn.addEventListener("click", () => {
            settingsPanel.classList.add("hidden");
        });
    }

    // -------------------------------------------------------------
    // Catalog Handling
    // -------------------------------------------------------------
    async function fetchCatalog(customPath = "", forceReindex = false) {
        productsGrid.innerHTML = '<div class="loading-state">Scanning catalog images...</div>';
        
        let url = "/api/catalog";
        const params = [];
        if (customPath) params.push(`path=${encodeURIComponent(customPath)}`);
        if (forceReindex) params.push(`force=true`);
        if (params.length) url += "?" + params.join("&");

        try {
            const res = await fetch(url).then(r => r.json());
            if (res.status === "success") {
                activeCatalog = res.products || [];
                if (catalogPathDisplay) catalogPathDisplay.textContent = res.catalog_path;
                if (catalogPathInput) catalogPathInput.value = customPath || "";
                renderCatalog(activeCatalog);
                
                // Parse URL params for retailer website iframe embeds
                const urlParams = new URLSearchParams(window.location.search);
                const embedParam = urlParams.get("embed");
                const productIdParam = urlParams.get("product_id");
                
                if (embedParam === "true" || productIdParam) {
                    document.body.classList.add("embed-mode");
                }
                
                if (productIdParam) {
                    const match = activeCatalog.find(p => p.id === productIdParam);
                    if (match) {
                        const cardDom = productsGrid.querySelector(`.product-card[data-id="${productIdParam}"]`);
                        if (cardDom) {
                            selectProduct(match, cardDom);
                        } else {
                            selectProduct(match, document.createElement("div"));
                        }
                    }
                }
            } else {
                productsGrid.innerHTML = `<div class="loading-state text-red">Scan Failed: ${res.detail || 'Error'}</div>`;
            }
        } catch (err) {
            console.error("Failed to load catalog", err);
            productsGrid.innerHTML = '<div class="loading-state text-red">Error loading catalog. Is server running?</div>';
        }
    }

    function renderCatalog(products) {
        if (!products.length) {
            productsGrid.innerHTML = '<div class="loading-state">No products found. Add items to catalog and click Scan.</div>';
            return;
        }

        productsGrid.innerHTML = "";
        products.forEach(p => {
            const isSelected = selectedProduct && selectedProduct.id === p.id;
            const card = document.createElement("div");
            card.dataset.category = p.category;
            card.dataset.id = p.id;
            
            if (isSelected) {
                card.className = "product-card group relative flex flex-col justify-between bg-[rgba(23,18,36,0.6)] border border-purple-400 ring-2 ring-purple-500/30 shadow-[0_0_20px_rgba(139,92,246,0.25)] rounded-2xl p-4 transition-all duration-300 cursor-pointer";
            } else {
                card.className = "product-card group relative flex flex-col justify-between bg-[rgba(23,18,36,0.6)] border border-purple-900/40 rounded-2xl p-4 transition-all duration-300 hover:border-purple-500/50 hover:shadow-[0_0_20px_rgba(139,92,246,0.15)] cursor-pointer";
            }

            card.innerHTML = `
                <div class="relative aspect-square w-full rounded-xl bg-neutral-100 overflow-hidden mb-3.5 flex items-center justify-center p-2.5 transition-transform duration-500 group-hover:scale-[1.02]">
                    <img src="${p.image_path}" alt="${p.id}" class="max-h-full max-w-full object-contain" onerror="this.src='data:image/svg+xml;utf8,<svg xmlns=%22http://www.w3.org/2000/svg%22 width=%22100%25%22 height=%22100%25%22><rect width=%22100%25%22 height=%22100%25%22 fill=%22%23222%22/><text x=%2250%25%22 y=%2250%25%22 font-size=%2214%22 text-anchor=%22middle%22 fill=%22%23aaa%22>Missing Image</text></svg>'">
                    <span class="absolute top-2.5 right-2.5 px-2 py-0.5 text-[10px] font-mono tracking-wider uppercase bg-black/75 text-purple-300 rounded border border-purple-500/30">${p.category}</span>
                </div>
                <div class="space-y-1 mb-3">
                    <div class="flex items-center justify-end">
                        <span class="text-[10px] bg-purple-500/10 text-purple-400 border border-purple-500/20 px-1.5 py-0.5 rounded-full uppercase">${p.category}</span>
                    </div>
                    <h4 class="text-sm font-semibold text-white tracking-tight">${p.id}</h4>
                </div>
                <button type="button" class="tryon-btn-action w-full py-2.5 px-4 rounded-xl text-xs font-semibold uppercase tracking-wider transition-all duration-300 flex items-center justify-center gap-1.5 cursor-pointer ${
                    isSelected
                        ? "border border-purple-400 bg-gradient-to-r from-purple-600 to-indigo-600 text-white shadow-[0_0_15px_rgba(139,92,246,0.5)]"
                        : "border border-indigo-500/20 bg-indigo-600 text-neutral-100 hover:text-white"
                }">
                    <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="w-3.5 h-3.5 ${isSelected ? 'animate-pulse text-yellow-300' : ''}"><path d="M12 3q1 4 4 6.5t3 5.5a1 1 0 0 1-14 0 5 5 0 0 1 1-3 1 1 0 0 0 5 0c0-2-1.5-3-1.5-5q0-2 2.5-4"/></svg>
                    <span>${isSelected ? "Tried On" : "Try On"}</span>
                </button>
            `;

            card.addEventListener("click", () => selectProduct(p, card));
            productsGrid.appendChild(card);
        });
    }

    function selectProduct(product, cardElement) {
        // Reset all card styles
        document.querySelectorAll(".product-card").forEach(c => {
            c.className = "product-card group relative flex flex-col justify-between bg-[rgba(23,18,36,0.6)] border border-purple-900/40 rounded-2xl p-4 transition-all duration-300 hover:border-purple-500/50 hover:shadow-[0_0_20px_rgba(139,92,246,0.15)] cursor-pointer";
            const btn = c.querySelector(".tryon-btn-action");
            if (btn) {
                btn.className = "tryon-btn-action w-full py-2.5 px-4 rounded-xl text-xs font-semibold uppercase tracking-wider transition-all duration-300 flex items-center justify-center gap-1.5 cursor-pointer border border-indigo-500/20 bg-indigo-600 text-neutral-100 hover:text-white";
                btn.innerHTML = `
                    <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="w-3.5 h-3.5"><path d="M12 3q1 4 4 6.5t3 5.5a1 1 0 0 1-14 0 5 5 0 0 1 1-3 1 1 0 0 0 5 0c0-2-1.5-3-1.5-5q0-2 2.5-4"/></svg>
                    <span>Try On</span>
                `;
            }
        });

        // Set active card style
        cardElement.className = "product-card group relative flex flex-col justify-between bg-[rgba(23,18,36,0.6)] border border-purple-400 ring-2 ring-purple-500/30 shadow-[0_0_20px_rgba(139,92,246,0.25)] rounded-2xl p-4 transition-all duration-300 cursor-pointer";
        const activeBtn = cardElement.querySelector(".tryon-btn-action");
        if (activeBtn) {
            activeBtn.className = "tryon-btn-action w-full py-2.5 px-4 rounded-xl text-xs font-semibold uppercase tracking-wider transition-all duration-300 flex items-center justify-center gap-1.5 cursor-pointer border border-purple-400 bg-gradient-to-r from-purple-600 to-indigo-600 text-white shadow-[0_0_15px_rgba(139,92,246,0.5)]";
            activeBtn.innerHTML = `
                <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="w-3.5 h-3.5 animate-pulse text-yellow-300"><path d="M12 3q1 4 4 6.5t3 5.5a1 1 0 0 1-14 0 5 5 0 0 1 1-3 1 1 0 0 0 5 0c0-2-1.5-3-1.5-5q0-2 2.5-4"/></svg>
                <span>Tried On</span>
            `;
        }

        selectedProduct = product;

        // Populate Selected Card Panel & Hanging Hanger
        if (resultProductImg) {
            resultProductImg.src = product.image_path;
        }
        
        const hangingCard = document.getElementById("hanging-garment-card");
        const hangingImg = document.getElementById("hanging-garment-img");
        if (hangingCard && hangingImg) {
            hangingImg.src = product.image_path;
            hangingCard.classList.remove("hidden");
        }

        if (productEmptyCard) productEmptyCard.classList.add("hidden");
        if (productSelectedCard) productSelectedCard.classList.remove("hidden");

        // Enable Action Buttons if user photo is uploaded
        updateStatusIndicator();
        runVideoBtn.setAttribute("disabled", "true");

        // Reset output container state
        resetOutput();
    }

    function resetOutput() {
        tryonOutputImg.classList.add("hidden");
        videoWrapper.classList.add("hidden");
        if (outputBadge) outputBadge.classList.add("hidden");
        tryonOutputImg.src = "";
        tryonOutputVideo.src = "";
        generatedImageBase64 = null;
        outputEmptyPlaceholder.classList.remove("hidden");
    }

    function clearSelection() {
        document.querySelectorAll(".product-card").forEach(c => {
            c.className = "product-card group relative flex flex-col justify-between bg-[rgba(23,18,36,0.6)] border border-purple-900/40 rounded-2xl p-4 transition-all duration-300 hover:border-purple-500/50 hover:shadow-[0_0_20px_rgba(139,92,246,0.15)] cursor-pointer";
            const btn = c.querySelector(".tryon-btn-action");
            if (btn) {
                btn.className = "tryon-btn-action w-full py-2.5 px-4 rounded-xl text-xs font-semibold uppercase tracking-wider transition-all duration-300 flex items-center justify-center gap-1.5 cursor-pointer border border-indigo-500/20 bg-indigo-600 text-neutral-100 hover:text-white";
                btn.innerHTML = `
                    <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="w-3.5 h-3.5"><path d="M12 3q1 4 4 6.5t3 5.5a1 1 0 0 1-14 0 5 5 0 0 1 1-3 1 1 0 0 0 5 0c0-2-1.5-3-1.5-5q0-2 2.5-4"/></svg>
                    <span>Try On</span>
                `;
            }
        });
        selectedProduct = null;

        if (productSelectedCard) productSelectedCard.classList.add("hidden");
        if (productEmptyCard) productEmptyCard.classList.remove("hidden");

        const hangingCard = document.getElementById("hanging-garment-card");
        if (hangingCard) {
            hangingCard.classList.add("hidden");
        }

        updateStatusIndicator();
        runVideoBtn.setAttribute("disabled", "true");

        resetOutput();
    }

    function updateStatusIndicator() {
        if (!vtoStepsIndicator) return;
        
        if (!userPhotoFile && !selectedProduct) {
            vtoStepsIndicator.textContent = "Upload photo & select garment to start";
            vtoStepsIndicator.className = "text-[10px] font-mono text-center text-neutral-500 uppercase tracking-widest mt-3 transition-colors duration-300";
            runTryonBtn.setAttribute("disabled", "true");
        } else if (userPhotoFile && !selectedProduct) {
            vtoStepsIndicator.textContent = "Select a garment from catalog";
            vtoStepsIndicator.className = "text-[10px] font-mono text-center text-purple-400/80 uppercase tracking-widest mt-3 transition-colors duration-300 animate-pulse";
            runTryonBtn.setAttribute("disabled", "true");
        } else if (!userPhotoFile && selectedProduct) {
            vtoStepsIndicator.textContent = "Upload your portrait photo";
            vtoStepsIndicator.className = "text-[10px] font-mono text-center text-indigo-400/80 uppercase tracking-widest mt-3 transition-colors duration-300 animate-pulse";
            runTryonBtn.setAttribute("disabled", "true");
        } else if (userPhotoFile && selectedProduct && !generatedImageBase64) {
            vtoStepsIndicator.textContent = "Click 'Try On Image' first";
            vtoStepsIndicator.className = "text-[10px] font-mono text-center text-emerald-400 uppercase tracking-widest mt-3 transition-colors duration-300 font-bold drop-shadow-[0_0_8px_rgba(16,185,129,0.3)] animate-pulse";
            runTryonBtn.removeAttribute("disabled");
        } else {
            vtoStepsIndicator.textContent = "Ready for Catwalk Video!";
            vtoStepsIndicator.className = "text-[10px] font-mono text-center text-sky-400 uppercase tracking-widest mt-3 transition-colors duration-300 font-bold drop-shadow-[0_0_8px_rgba(0,210,255,0.3)]";
            runTryonBtn.removeAttribute("disabled");
        }
    }

    if (clearProductSelection) {
        clearProductSelection.addEventListener("click", clearSelection);
    }

    if (scanCatalogBtn) {
        scanCatalogBtn.addEventListener("click", () => {
            const path = catalogPathInput.value.trim();
            fetchCatalog(path, true);
        });
    }

    // -------------------------------------------------------------
    // Category Pills Filtering
    // -------------------------------------------------------------
    function setupPills() {
        categoryFilterPills.forEach(pill => {
            pill.addEventListener("click", () => {
                categoryFilterPills.forEach(p => p.classList.remove("active"));
                pill.classList.add("active");

                const cat = pill.dataset.category;
                const cards = document.querySelectorAll(".product-card");
                cards.forEach(card => {
                    if (cat === "All" || card.dataset.category === cat) {
                        card.classList.remove("hidden");
                    } else {
                        card.classList.add("hidden");
                    }
                });
            });
        });
    }

    // -------------------------------------------------------------
    // User Photo Handling
    // -------------------------------------------------------------
    function setupDropzone() {
        uploadZone.addEventListener("click", (e) => {
            if (e.target !== replacePhotoBtn) {
                userFileInput.click();
            }
        });

        replacePhotoBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            clearUserPhoto();
        });

        userFileInput.addEventListener("change", (e) => {
            if (e.target.files && e.target.files[0]) {
                handleUserImageFile(e.target.files[0]);
            }
        });

        uploadZone.addEventListener("dragover", (e) => {
            e.preventDefault();
            uploadZone.classList.add("hover");
        });

        uploadZone.addEventListener("dragleave", () => {
            uploadZone.classList.remove("hover");
        });

        uploadZone.addEventListener("drop", (e) => {
            e.preventDefault();
            uploadZone.classList.remove("hover");
            if (e.dataTransfer.files && e.dataTransfer.files[0]) {
                handleUserImageFile(e.dataTransfer.files[0]);
            }
        });
    }

    function clearUserPhoto() {
        userPhotoFile = null;
        userPhotoPreview.src = "";
        userPhotoPreview.classList.add("hidden");
        dropzonePromptContainer.classList.remove("hidden");
        photoHoverOverlay.classList.add("hidden");
        userFileInput.value = "";
        
        updateStatusIndicator();
        runVideoBtn.setAttribute("disabled", "true");
        resetOutput();
    }

    function handleUserImageFile(file) {
        userPhotoFile = file;

        const reader = new FileReader();
        reader.onload = (e) => {
            userPhotoPreview.src = e.target.result;
            userPhotoPreview.classList.remove("hidden");
            dropzonePromptContainer.classList.add("hidden");
            photoHoverOverlay.classList.remove("hidden");

            const filenameLabel = document.getElementById("user-filename-label");
            if (filenameLabel) filenameLabel.textContent = file.name;

            updateStatusIndicator();
        };
        reader.readAsDataURL(file);
    }

    // -------------------------------------------------------------
    // VTO Processors Execution
    // -------------------------------------------------------------
    runTryonBtn.addEventListener("click", async () => {
        console.log("Try On button clicked! selectedProduct:", selectedProduct, "userPhotoFile:", userPhotoFile);
        if (!selectedProduct) {
            console.warn("Try On click ignored: selectedProduct is null!");
            return;
        }

        // Reset Output & Show loading
        outputEmptyPlaceholder.classList.add("hidden");
        tryonOutputImg.classList.add("hidden");
        videoWrapper.classList.add("hidden");
        if (outputBadge) outputBadge.classList.add("hidden");
        
        tryonLoading.classList.remove("hidden");
        laserScanner.classList.remove("hidden");
        loadingText.textContent = "Materializing Fabric...";
        
        runTryonBtn.setAttribute("disabled", "true");
        runVideoBtn.setAttribute("disabled", "true");

        const formData = new FormData();
        formData.append("product_id", selectedProduct.id);
        formData.append("product_category", selectedProduct.category);
        formData.append("product_description", selectedProduct.description || "");
        formData.append("product_image_path", selectedProduct.image_path);

        try {
            if (userPhotoFile) {
                formData.append("user_photo", userPhotoFile);
            } else {
                alert("Please upload your portrait photo first!");
                tryonLoading.classList.add("hidden");
                laserScanner.classList.add("hidden");
                outputEmptyPlaceholder.classList.remove("hidden");
                runTryonBtn.removeAttribute("disabled");
                return;
            }

            const res = await fetch("/api/tryon", {
                method: "POST",
                body: formData
            }).then(r => r.json());

            tryonLoading.classList.add("hidden");
            laserScanner.classList.add("hidden");
            runTryonBtn.removeAttribute("disabled");

            if (res.status === "success") {
                generatedImageBase64 = res.image_base64;
                tryonOutputImg.src = res.image_base64;
                tryonOutputImg.classList.remove("hidden");
                
                // Show badge
                if (outputBadge) {
                    outputBadge.textContent = "FLASH SYNTH";
                    outputBadge.className = "badge-cyber";
                    outputBadge.classList.remove("hidden");
                }
                
                configModel.textContent = res.model_used;

                // Enable video generation button
                runVideoBtn.removeAttribute("disabled");
                updateStatusIndicator();
            } else {
                alert("Try-On Generation Error: " + (res.detail || "Request failed"));
                outputEmptyPlaceholder.classList.remove("hidden");
            }
        } catch (err) {
            console.error("VTO generation failed", err);
            tryonLoading.classList.add("hidden");
            laserScanner.classList.add("hidden");
            outputEmptyPlaceholder.classList.remove("hidden");
            runTryonBtn.removeAttribute("disabled");
            alert("Connection error when calling try-on API.");
        }
    });

    runVideoBtn.addEventListener("click", async () => {
        console.log("Video button clicked! generatedImageBase64 present:", !!generatedImageBase64);
        if (!generatedImageBase64) {
            console.warn("Video click ignored: generatedImageBase64 is empty!");
            return;
        }

        tryonOutputImg.classList.add("hidden");
        if (outputBadge) outputBadge.classList.add("hidden");
        
        tryonLoading.classList.remove("hidden");
        laserScanner.classList.remove("hidden");
        loadingText.textContent = "Rendering Physics...";
        
        runTryonBtn.setAttribute("disabled", "true");
        runVideoBtn.setAttribute("disabled", "true");

        try {
            const res = await fetch("/api/video", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    tryon_image_base64: generatedImageBase64,
                    scene_description: `a model wearing ${selectedProduct.id} walking on a clean studio catwalk`
                })
            }).then(r => r.json());

            tryonLoading.classList.add("hidden");
            laserScanner.classList.add("hidden");
            runTryonBtn.removeAttribute("disabled");

            if (res.status === "success") {
                outputEmptyPlaceholder.classList.add("hidden");
                tryonOutputVideo.src = res.video_base64;
                videoWrapper.classList.remove("hidden");

                // Show badge
                if (outputBadge) {
                    outputBadge.textContent = "VEO STREAM";
                    outputBadge.className = "badge-cyber";
                    outputBadge.classList.remove("hidden");
                }
                // Re-enable video button so the user can regenerate the catwalk.
                runVideoBtn.removeAttribute("disabled");
            } else {
                alert("Catwalk Video Error: " + (res.detail || "Request failed"));
                tryonOutputImg.classList.remove("hidden"); // fall back to image
                if (outputBadge) {
                    outputBadge.textContent = "FLASH SYNTH";
                    outputBadge.className = "badge-cyber";
                    outputBadge.classList.remove("hidden");
                }
                runVideoBtn.removeAttribute("disabled");
            }
        } catch (err) {
            console.error("Video VTO failed", err);
            tryonLoading.classList.add("hidden");
            laserScanner.classList.add("hidden");
            tryonOutputImg.classList.remove("hidden");
            if (outputBadge) {
                outputBadge.textContent = "FLASH SYNTH";
                outputBadge.className = "badge-cyber";
                outputBadge.classList.remove("hidden");
            }
            runTryonBtn.removeAttribute("disabled");
            runVideoBtn.removeAttribute("disabled");
            alert("Connection error when calling video generation API.");
        }
    });
});
