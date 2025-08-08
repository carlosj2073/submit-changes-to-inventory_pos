#
#
#
#
#
@login_required
def sample_dashboard(request):
    """
    This view now only renders the dashboard HTML page.
    All KPI data is fetched by the frontend via the new API endpoint.
    """
    company, user_profile, has_company = get_user_company(request)
    if not has_company:
        messages.error(request, "Company not found. Please set up your company first.")
        return redirect(reverse('accounts:company_setup'))
    
    context = {
        'company': company,
    }
    return render(request, 'your_analytics_insights_page.html', context)

@login_required
def get_kpi_data(request):
    """
    API endpoint to fetch all KPI data as a single JSON object.
    """
    company, user_profile, has_company = get_user_company(request)
    if not has_company:
        return JsonResponse({'error': 'Company not found.'}, status=403)

    # --- Date Definitions (All timezone-aware for consistency with DateTimeField) ---
    today_aware = timezone.now()
    start_of_today_aware = today_aware.replace(hour=0, minute=0, second=0, microsecond=0)
    thirty_days_ago_aware = start_of_today_aware - timedelta(days=30)

    # All Order_Items for this company with 'paid' status
    all_time_order_items = Order_Items.objects.filter(
        order__company=company,
        order__status='paid'
    )
    
    # --- KPI Calculations ---
    # Total Revenue (All Time)
    total_sales = all_time_order_items.aggregate(
        sum_sales=Coalesce(Sum(F('quantity') * F('price')), Decimal('0.00'))
    )['sum_sales']

    # Total Profit (All Time)
    total_profit_all_time = all_time_order_items.aggregate(
        total=Coalesce(Sum(F('net_profit')), Decimal('0.00'))
    )['total']

    # Total Orders (All Time)
    total_orders = Orders.objects.filter(company=company, status='paid').count()

    # Gross Profit Margin (Overall)
    overall_revenue = all_time_order_items.aggregate(
        total=Coalesce(Sum(F('quantity') * F('price')), Decimal('0.00'))
    )['total']
    overall_cogs = all_time_order_items.aggregate(
        total=Coalesce(Sum(F('cogs')), Decimal('0.00'))
    )['total']
    gross_profit_margin = Decimal('0.00')
    if overall_revenue > 0:
        gross_profit_margin = ((overall_revenue - overall_cogs) / overall_revenue) * 100

    # Number of Items Selling Well (Last 30 days - KPI)
    num_items_selling_well = Order_Items.objects.filter(
        order__company=company,
        order__order_date__gte=thirty_days_ago_aware,
        order__status='paid'
    ).values('product').distinct().count()

    # Total Inventory Value
    inventory_products = Product.objects.filter(company=company)
    total_inventory_value = inventory_products.aggregate(
        total_value=Coalesce(Sum(F('stock') * F('price')), Decimal('0.00'))
    )['total_value']

    # Items Needing Attention
    low_stock_products = Product.objects.filter(
        company=company,
        stock__lte=F('low_stock_input')
    )
    recent_sales_products_ids = Order_Items.objects.filter(
        order__company=company,
        order__order_date__gte=thirty_days_ago_aware,
        order__status='paid'
    ).values_list('product_id', flat=True).distinct()
    not_selling_products = Product.objects.filter(
        company=company
    ).exclude(
        id__in=recent_sales_products_ids
    )
    items_needing_attention_count = low_stock_products.count() + not_selling_products.exclude(id__in=low_stock_products.values_list('id', flat=True)).count()

    # Return as JSON
    return JsonResponse({
        'total_sales': float(total_sales),
        'total_profit': float(total_profit_all_time),
        'total_orders': total_orders,
        'gross_profit_margin': float(gross_profit_margin),
        'num_items_selling_well': num_items_selling_well,
        'total_inventory_value': float(total_inventory_value),
        'items_needing_attention_count': items_needing_attention_count,
    })

@login_required
def get_items_selling_well_modal_content(request):
    company, user_profile_obj, has_company = get_user_company(request)

    if not has_company:
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return HttpResponse("<p class='text-danger text-center'>Company not found. Please set up your company.</p>", status=403)
        else:
            messages.error(request, "Company not found. Please set up your company first.")
            return redirect(reverse('accounts:company_setup'))

    # --- CORRECTED: Use timezone.now() for precise datetime filtering ---
    now_aware = timezone.now()
    end_date_for_filter = now_aware # Filter up to the current moment
    start_date_for_filter = now_aware - timedelta(days=30) # Exactly 30 days ago from now

    top_selling_items = Order_Items.objects.filter(
        order__company=company,
        order__order_date__gte=start_date_for_filter, # Use >= for start datetime
        order__order_date__lte=end_date_for_filter,   # Use <= for end datetime
        order__status='paid' # Only consider paid orders
    ).values(
        'product__name',
        'product__barcode',
        'product__stock', # Fetch current stock for display
    ).annotate(
        total_quantity_sold=Sum('quantity'),
        total_revenue_from_item=Sum(F('quantity') * F('price')), # F('price') refers to Order_Items.price
        total_profit_from_item=Sum(
            ExpressionWrapper(
                F('quantity') * (F('price') - F('product__cost')), # F('product__cost') is from Product model
                output_field=DecimalField()
            )
        )
    ).order_by('-total_quantity_sold')[:10] # Top 10 items

    context = {
        'top_selling_items': top_selling_items,
        # Pass the original date objects for display in the template's header
        'start_date': timezone.localdate(start_date_for_filter),
        'end_date': timezone.localdate(end_date_for_filter),
    }

    # Ensure this renders the correct template path:
    return render(request, 'items_selling_well_modal_content.html', context)

@login_required
def items_to_sell_modal_view(request):
    """
    Fetches products that need attention for the authenticated user's company
    (running low or not selling) and renders a partial HTML for the modal.
    """
    company, user_profile, has_company = get_user_company(request)
    if not has_company:
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return HttpResponse("<p class='text-danger text-center'>Company not found. Please set up your company.</p>", status=403)
        else:
            messages.error(request, "Company not found. Please set up your company first.")
            return redirect(reverse('accounts:company_setup'))

    # Define date ranges
    today = timezone.localdate(timezone.now())
    ninety_days_ago = today - timedelta(days=90)
    
    # Efficiently get all products sold in the last 90 days.
    # This avoids a subquery inside the main queryset, which is more performant.
    sold_in_last_90_days_product_ids = set(Order_Items.objects.filter(
        order__company=company,
        order__status='paid',
        order__order_date__gte=ninety_days_ago
    ).values_list('product_id', flat=True))

    # Build a base queryset of all products for the company
    products_queryset = Product.objects.filter(company=company)

    # --- Filtering Logic ---
    low_stock_filter = Q(stock__lte=F('low_stock_input'))
    
    # "Not selling" filter: has stock > 0 AND has not sold in the last 90 days
    not_selling_filter = Q(stock__gt=0) & ~Q(id__in=sold_in_last_90_days_product_ids)

    # Combine the filters
    attention_needed_products = products_queryset.filter(low_stock_filter | not_selling_filter)

    # Apply search query filter if 'q' parameter is present
    query = request.GET.get('q', '')
    if query:
        attention_needed_products = attention_needed_products.filter(
            Q(name__icontains=query) |
            Q(barcode__icontains=query)
        )

    # Annotate with the specific warning status for display
    final_products = attention_needed_products.annotate(
        warning_status=Case(
            When(low_stock_filter & not_selling_filter, then=Value('Running Low & Not Selling')),
            When(low_stock_filter, then=Value('Running Low')),
            When(not_selling_filter, then=Value('Not Selling')),
            default=Value(''),
            output_field=CharField()
        )
    ).order_by(
        Case(
            When(warning_status='Running Low & Not Selling', then=0),
            When(warning_status='Running Low', then=1),
            When(warning_status='Not Selling', then=2),
            default=3,
            output_field=IntegerField(),
        ),
        'stock',
        'name'
    )
    
    # Note: We can simplify this logic since all products here have a warning.
    # The default case is technically unreachable if the filters are applied correctly.

    context = {
        'items': final_products,
        'search_query': query,
        'page_title': "Items Needing Attention",
    }

    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        html_content = render_to_string('items_attention_modal_content.html', context, request=request)
        return HttpResponse(html_content)
    else:
        messages.info(request, "Items needing attention are typically viewed within the dashboard modal.")
        return redirect(reverse('your_analytics_insights'))

@login_required
def profit_trends_view(request):
    """
    Displays historical monthly profit trends for the authenticated user's company.
    Queries the CompanyMonthlyMetric model for pre-aggregated data.
    Can render as a full page or as a partial for a modal via AJAX.
    """
    company, user_profile, has_company = get_user_company(request)
    if not has_company:
        messages.error(request, "Company not found. Please set up your company first to view profit trends.")
        return redirect(reverse('accounts:company_setup'))

    historical_data = CompanyMonthlyMetric.objects.filter(company=company).order_by('year', 'month')

    chart_labels = []
    profit_data = []
    revenue_data = []
    cogs_data = []

    for entry in historical_data:
        chart_labels.append(entry.date_recorded.strftime("%b %Y") if entry.date_recorded else f"{entry.month}/{entry.year}")
        profit_data.append(float(entry.net_monthly_profit))
        revenue_data.append(float(entry.total_monthly_revenue))
        cogs_data.append(float(entry.total_monthly_cogs))

    context = {
        'company': company,
        'historical_metrics': historical_data,
        'chart_labels': chart_labels,
        'profit_data': profit_data,
        'revenue_data': revenue_data,
        'cogs_data': cogs_data,
        'page_title': "Historical Profit Trends"
    }

    # Detect if the request is an AJAX request (commonly done by 'X-Requested-With' header)
    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        # For AJAX requests, render the partial content template (your current profit_trends.html)
        html_content = render_to_string('profit_trends.html', context, request=request)
        return HttpResponse(html_content)
    else:
        # For regular (non-AJAX) requests, render the new full dashboard template
        return render(request, 'profit_trends_dashboard.html', context)
    
# inventory_app/views.py

# ... (rest of your imports and other views)
@login_required
def total_inventory_value_modal_view(request):
    company, user_profile, has_company = get_user_company(request)

    if not has_company:
        messages.error(request, "Company not found for the current user. Please set up your company.")
        return redirect(reverse('your_analytics_insights')) # Ensure this matches your URL name

    # Base queryset to filter products that are in stock and have a price
    base_queryset = Product.objects.filter(
        company=company,
        stock__gt=0,
        price__gt=0
    )

    # --- Overall Totals ---
    total_retail_value = base_queryset.aggregate(
        total=Sum(F('stock') * F('price'))
    )['total'] or 0.0

    total_cost_value = base_queryset.aggregate(
        total=Sum(F('stock') * F('cost'))
    )['total'] or 0.0

    # --- Combined Breakdown by Category ---
    combined_category_breakdown = list(base_queryset.values('category__name').annotate(
        retail_value=Sum(F('stock') * F('price')),
        cost_value=Sum(F('stock') * F('cost'))
    ).order_by('category__name'))

    # --- Combined Breakdown by Supplier ---
    combined_supplier_breakdown = list(base_queryset.values('supplier__name').annotate(
        retail_value=Sum(F('stock') * F('price')),
        cost_value=Sum(F('stock') * F('cost'))
    ).order_by('supplier__name'))

    context = {
        'total_inventory_retail_value': total_retail_value,
        'total_inventory_cost_value': total_cost_value,
        'combined_category_breakdown': combined_category_breakdown,
        'combined_supplier_breakdown': combined_supplier_breakdown,
        'company': company,
    }

    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        return render(request, 'total_inventory_modal.html', context)
    else:
        messages.warning(request, "This page is intended to be loaded via AJAX.")
        return redirect(reverse('your_analytics_insights')) # Ensure this matches your URL name


@login_required
def get_dashboard_graph_data(request):
    """
    Provides data for the main dashboard sales graph.
    The data is based on the selected metric and time period,
    aggregating from Order_Items and Orders models.
    """
    metric = request.GET.get('metric', 'sales')
    time_period = request.GET.get('time_period', 'month')

    # Ensure the user is associated with a company
    try:
        company = request.user.profile.company
    except (AttributeError, Companies.DoesNotExist):
        return JsonResponse({'error': 'Company not found for user profile'}, status=400)

    # Dictionary to hold the final data for JSON response
    response_data = {
        'labels': [],
        'data': [],
        'metric_label': '',
        'title_suffix': '',
        'metric_type': 'currency'
    }

    today = timezone.now()

    # Define the base queryset for Order_Items, filtered by company and paid status
    base_order_items_query = Order_Items.objects.filter(
        order__company=company,
        order__status='paid'
    )

    # --- Step 1: Determine Date Range and Truncation Level ---
    trunc_level = TruncDay
    title_suffix = ""
    start_date = None

    if time_period == 'week':
        start_date = today - timedelta(days=6)
        trunc_level = TruncDay
        title_suffix = "for the Last 7 Days"
    elif time_period == 'month':
        start_date = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        trunc_level = TruncDay
        title_suffix = f"for {today.strftime('%B %Y')}"
    elif time_period == 'quarter':
        start_date = today - timedelta(days=89)
        trunc_level = TruncWeek
        title_suffix = "for the Last 90 Days"
    elif time_period == 'year':
        start_date = today.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        trunc_level = TruncMonth
        title_suffix = f"for {today.year}"
    elif time_period == 'all':
        first_sale = base_order_items_query.order_by('order__order_date').first()
        if first_sale:
            start_date = first_sale.order.order_date
        else:
            return JsonResponse(response_data)
        trunc_level = TruncMonth
        title_suffix = "Overall"
    else:
        return JsonResponse({'error': 'Invalid time_period'}, status=400)

    # Filter the query by the determined date range
    if start_date:
        base_order_items_query = base_order_items_query.filter(order__order_date__gte=start_date)

    # --- Step 2: Aggregate Data Based on Metric ---
    aggregated_data = []
    metric_label = ""
    metric_type = "currency"

    if metric == 'sales':
        aggregated_data = base_order_items_query.annotate(
            period=trunc_level('order__order_date')
        ).values('period').annotate(
            value=Sum(F('quantity') * F('price'))
        ).order_by('period')
        metric_label = "Total Sales Revenue"

    elif metric == 'profit':
        aggregated_data = base_order_items_query.annotate(
            period=trunc_level('order__order_date')
        ).values('period').annotate(
            value=Sum(
                ExpressionWrapper(
                    F('quantity') * (F('price') - F('product__cost')),
                    output_field=DecimalField()
                )
            )
        ).order_by('period')
        metric_label = "Total Profit"

    elif metric == 'gross_profit_margin':
        aggregated_data = base_order_items_query.annotate(
            period=trunc_level('order__order_date')
        ).values('period').annotate(
            total_revenue=Sum(F('quantity') * F('price')),
            total_cogs=Sum(F('quantity') * F('product__cost'))
        ).order_by('period')

        processed_data = []
        for item in aggregated_data:
            revenue = item['total_revenue'] if item['total_revenue'] is not None else 0
            cogs = item['total_cogs'] if item['total_cogs'] is not None else 0
            if revenue > 0:
                margin = ((revenue - cogs) / revenue) * 100
            else:
                margin = 0
            processed_data.append({'period': item['period'], 'value': margin})
        aggregated_data = processed_data

        metric_label = "Gross Profit Margin"
        metric_type = "percentage"

    elif metric == 'num_orders':
        # This requires a separate query on the Orders model
        base_orders_query = Orders.objects.filter(
            company=company,
            status='paid'
        )
        if start_date:
            base_orders_query = base_orders_query.filter(order_date__gte=start_date)

        aggregated_data = base_orders_query.annotate(
            period=trunc_level('order_date')
        ).values('period').annotate(
            value=Count('id')
        ).order_by('period')
        metric_label = "Number of Orders"
        metric_type = "integer"

    else:
        return JsonResponse({'error': 'Invalid metric'}, status=400)

    # --- Step 3: Generate Labels and Fill Data (Ensuring Continuity) ---
    data_map = {item['period']: float(item['value'] if item['value'] is not None else 0) for item in aggregated_data}

    current_iter_date = start_date
    while current_iter_date <= today:
        period_key = None
        if trunc_level == TruncDay:
            period_key = current_iter_date.replace(hour=0, minute=0, second=0, microsecond=0)
            response_data['labels'].append(current_iter_date.strftime('%b %d'))
            current_iter_date += timedelta(days=1)
        elif trunc_level == TruncWeek:
            period_key = current_iter_date.replace(hour=0, minute=0, second=0, microsecond=0)
            response_data['labels'].append(f"Wk {current_iter_date.isocalendar().week} ({current_iter_date.strftime('%b %d')})")
            current_iter_date += timedelta(days=7)
        elif trunc_level == TruncMonth:
            period_key = current_iter_date.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            response_data['labels'].append(current_iter_date.strftime('%b %Y'))
            current_iter_date += relativedelta(months=1)
        elif trunc_level == TruncYear:
            period_key = current_iter_date.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            response_data['labels'].append(current_iter_date.strftime('%Y'))
            current_iter_date += relativedelta(years=1)

        response_data['data'].append(data_map.get(period_key, 0.0))

    response_data['metric_label'] = metric_label
    response_data['title_suffix'] = title_suffix
    response_data['metric_type'] = metric_type

    return JsonResponse(response_data)


def get_graph_customization_modal_content(request):
    """
    Renders the HTML content for the graph customization modal.
    This view should be fetched via AJAX to populate the modal.
    """
    return render(request, 'graph_customization_modal_content.html', {})

# --- Historical Trends API for Dashboard (Last 10 Months) ---

@login_required
def get_sales_trends_api_data(request, company_id):
    """
    Provides data for the dashboard's 10-month summary table,
    displaying columns based on 'metrics' GET parameter.
    If no sales history, displays current month with zeros.
    """
    # (Permission check commented out as per your request)

    now_aware = timezone.now()
    today_start_of_month_aware = now_aware.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    default_start_date_aware = (today_start_of_month_aware - relativedelta(months=9))

    first_metric_entry = CompanyMonthlyMetric.objects.filter(
        company_id=company_id
    ).order_by('year', 'month').first()

    actual_query_start_year = default_start_date_aware.year
    actual_query_start_month = default_start_date_aware.month

    if first_metric_entry:
        first_entry_date = datetime(first_metric_entry.year, first_metric_entry.month, 1, tzinfo=timezone.get_current_timezone())
        if first_entry_date > default_start_date_aware:
            actual_query_start_year = first_metric_entry.year
            actual_query_start_month = first_metric_entry.month
    else:
        actual_query_start_year = today_start_of_month_aware.year
        actual_query_start_month = today_start_of_month_aware.month
        
    sales_data = CompanyMonthlyMetric.objects.filter(
        company_id=company_id,
        year__gte=actual_query_start_year,
    ).filter(
        Q(year=actual_query_start_year, month__gte=actual_query_start_month) | 
        Q(year__gt=actual_query_start_year)
    ).order_by('year', 'month')

    all_months = []
    current_month_iter_aware = datetime(actual_query_start_year, actual_query_start_month, 1, tzinfo=timezone.get_current_timezone())
    
    while current_month_iter_aware <= today_start_of_month_aware:
        all_months.append(current_month_iter_aware.date())
        current_month_iter_aware += relativedelta(months=1)

    sales_dict = {}
    for s in sales_data:
        month_date_key = date(s.year, s.month, 1)
        sales_dict[month_date_key] = {
            'revenue': s.total_monthly_revenue, # Changed to 'revenue' for consistency with metrics_order
            'net_profit': s.net_monthly_profit, # Changed to 'net_profit'
            'quantity_sold': s.total_products_sold,
            'cogs': s.total_monthly_cogs,
        }

    trend_data = []
    
    # --- CHANGE START ---
    # Get selected metrics from GET parameter. Default to all if not provided.
    requested_metrics_str = request.GET.get('metrics', 'revenue,net_profit,quantity_sold,cogs')
    metrics_order = [m.strip() for m in requested_metrics_str.split(',') if m.strip()]
    # Ensure a default if the parameter is empty or invalid
    if not metrics_order:
        metrics_order = ['revenue', 'net_profit', 'quantity_sold', 'cogs']
    # --- CHANGE END ---

    for month_dt in all_months:
        month_key = month_dt 
        month_year_str = month_key.strftime('%b %Y')
        row_data = {'period': month_year_str}

        entry = sales_dict.get(month_key, {}) 

        for metric in metrics_order:
            # Ensure the key in `entry` matches the string in `metrics_order`
            # For CompanyMonthlyMetric, the fields are `total_monthly_revenue`, `net_monthly_profit`, etc.
            # We need to map these to the simpler 'revenue', 'net_profit' keys used in `metrics_order`
            if metric == 'revenue':
                row_data[metric] = float(entry.get('revenue', 0.0))
            elif metric == 'net_profit':
                row_data[metric] = float(entry.get('net_profit', 0.0))
            elif metric == 'quantity_sold':
                row_data[metric] = int(entry.get('quantity_sold', 0))
            elif metric == 'cogs':
                row_data[metric] = float(entry.get('cogs', 0.0))

        trend_data.append(row_data)

    return JsonResponse({'data': trend_data, 'metrics_order': metrics_order})


# --- All Monthly Sales Trends API (Historical) ---
@login_required
@login_required
@login_required
def get_all_monthly_sales_trends_api_data(request, company_id):
    """
    Provides all historical data for the company for the modal.
    If no sales history, displays current month with zeros.
    """
    
    # New, more robust permission check
    # Safely retrieve the user's UserProfile object or raise a 404
    user_profile = get_object_or_404(UserProfile, user=request.user)

    # Then, attempt to find the company and check the employee relationship
    try:
        company = Companies.objects.get(id=company_id)
        if not company.employees.filter(id=user_profile.id).exists():
            return JsonResponse({'error': 'Unauthorized access to company data.'}, status=403)
    except Companies.DoesNotExist:
        return JsonResponse({'error': 'Company not found.'}, status=403)
    
    # --- Your original view logic continues here ---
    metrics_param = request.GET.get('metrics', 'revenue')

    # Find the earliest and latest sale dates for the company
    first_sale = Order_Items.objects.filter(
        order__company_id=company_id
    ).order_by('order__order_date').first()

    last_sale = Order_Items.objects.filter(
        order__company_id=company_id
    ).order_by('order__order_date').last()

    all_months = []
    query_start_date_aware = None 

    now_aware = timezone.now()
    today_start_of_month_aware = now_aware.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    if first_sale and last_sale:
        current_month_iter_aware = first_sale.order.order_date.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        
        last_month_in_history_aware = last_sale.order.order_date.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        loop_end_month_aware = max(today_start_of_month_aware, last_month_in_history_aware)

        query_start_date_aware = current_month_iter_aware
        
        while current_month_iter_aware <= loop_end_month_aware:
            all_months.append(current_month_iter_aware.date())
            current_month_iter_aware += relativedelta(months=1)
    else:
        # --- NEW USER / NO SALES DATA YET: Display only the current month ---
        all_months.append(today_start_of_month_aware.date())
        # Query will effectively fetch data for this month if any, but will be empty.
        query_start_date_aware = today_start_of_month_aware 

    # --- Query to get sales data ---
    sales_data_query = Order_Items.objects.filter(
        order__company_id=company_id,
        order__order_date__gte=query_start_date_aware
    ).annotate(
        period=TruncMonth('order__order_date'),
        total_revenue=Sum('price'),
        total_net_profit=Sum('net_profit'),
        total_quantity_sold=Sum('quantity'),
        total_cogs=Sum('cogs')
    ).values('period', 'total_revenue', 'total_net_profit', 'total_quantity_sold', 'total_cogs').order_by('period')

    # --- Map sales data to months ---
    sales_dict = {s['period'].date(): s for s in sales_data_query}

    trend_data = []
    metrics_order = []

    requested_metrics = metrics_param.split(',')

    if 'revenue' in requested_metrics or 'all' in requested_metrics:
        metrics_order.append('revenue')
    if 'net_profit' in requested_metrics or 'all' in requested_metrics:
        metrics_order.append('net_profit')
    if 'quantity_sold' in requested_metrics or 'all' in requested_metrics:
        metrics_order.append('quantity_sold')
    if 'cogs' in requested_metrics or 'all' in requested_metrics:
        metrics_order.append('cogs')

    for month_dt in all_months:
        month_key = month_dt
        month_year_str = month_key.strftime('%b %Y')
        row_data = {'period': month_year_str}

        entry = sales_dict.get(month_key, {})

        for metric in metrics_order:
            if metric == 'revenue':
                row_data[metric] = float(entry.get('total_revenue', 0.0))
            elif metric == 'net_profit':
                row_data[metric] = float(entry.get('total_net_profit', 0.0))
            elif metric == 'quantity_sold':
                row_data[metric] = int(entry.get('total_quantity_sold', 0))
            elif metric == 'cogs':
                row_data[metric] = float(entry.get('total_cogs', 0.0))

        trend_data.append(row_data)

    return JsonResponse({'data': trend_data, 'metrics_order': metrics_order})
@login_required
def historical_trends_modal_content(request):
    """
    This view renders the updated modal content file,
    and can pass the current dashboard's displayed metrics for pre-selection.
    """
    # Get the current dashboard metrics from the GET parameters if available
    # This assumes the main dashboard page will pass its current metrics to this modal URL
    current_dashboard_metrics_str = request.GET.get('current_metrics', 'revenue,net_profit,quantity_sold,cogs')
    current_dashboard_metrics_list = [m.strip() for m in current_dashboard_metrics_str.split(',') if m.strip()]
    if not current_dashboard_metrics_list:
        current_dashboard_metrics_list = ['revenue', 'net_profit', 'quantity_sold', 'cogs']

    context = {
        'current_dashboard_metrics_list': current_dashboard_metrics_list, # Pass this for pre-checking
        'all_possible_metrics': [
            {'name': 'Revenue', 'key': 'revenue'},
            {'name': 'Net Profit', 'key': 'net_profit'},
            {'name': 'Quantity Sold', 'key': 'quantity_sold'},
            {'name': 'COGS', 'key': 'cogs'},
        ]
    }
    return render(request, 'historical_trends_modal.html', context)



def get_user_company(request):
    """
    Retrieves the authenticated user's profile and associated company.
    Returns (company_object, user_profile_object, has_company_linked_boolean).
    """
    if not request.user.is_authenticated:
        return None, None, False

    try:
        # CORRECTED: Access profile via 'profile' related_name
        profile = request.user.profile
        if profile.company:
            return profile.company, profile, True
        else:
            return None, profile, False # Return profile even if no company linked
    except UserProfile.DoesNotExist:
        # This can happen if a User exists but no UserProfile was created for them.
        # The post_save signal should prevent this, but it's good for robustness.
        return None, None, False
    except AttributeError:
        # This can happen if 'profile' attribute is not yet attached (e.g., during initial setup)
        # or if request.user is an AnonymousUser.
        return None, None, False

