"""Atera integration tools for creating customers, contacts, and tickets."""

import logging
import asyncio
from typing import Optional
from enuclea.atera_tool import AteraClient, load_atera_credentials

logger = logging.getLogger(__name__)

def create_atera_customer(customer_name: str, domain: Optional[str] = None) -> str:
    """Creates a new customer (client) in the Atera platform.
    
    Args:
        customer_name: The name of the customer organization to create.
        domain: Optional primary domain for the customer.
        
    Returns:
        str: A summary message of the creation result, including the new CustomerID.
    """
    api_key, base_url = load_atera_credentials()
    if not api_key:
        return "Error: Atera API credentials not configured in environment."

    async def _run():
        payload = {"CustomerName": customer_name}
        if domain:
            payload["Domain"] = domain
            
        async with AteraClient(api_key, base_url) as client:
            try:
                # 1. Check if customer already exists (idempotency check)
                page = 1
                while True:
                    data = await client._request("GET", "customers", params={"page": page, "itemsInPage": 50})
                    if not data:
                        break
                    items = data.get("items", [])
                    for c in items:
                        if c.get("CustomerName", "").strip().lower() == customer_name.strip().lower():
                            return f"Customer '{customer_name}' already exists with CustomerID: {c.get('CustomerID')}."
                    if len(items) < 50:
                        break
                    page += 1
                
                # 2. Create customer
                res = await client._request("POST", "customers", json_payload=payload)
                if isinstance(res, dict) and "ActionID" in res:
                    return f"Successfully created customer '{customer_name}' with CustomerID: {res['ActionID']}."
                return f"Successfully created customer '{customer_name}'. Response: {res}"
            except Exception as e:
                return f"Error creating customer: {e}"

    return asyncio.run(_run())

def create_atera_contact(
    customer_id: int, 
    firstname: str, 
    lastname: str, 
    email: str, 
    job_title: Optional[str] = None, 
    phone: Optional[str] = None, 
    mobile_phone: Optional[str] = None
) -> str:
    """Creates a new contact (EndUser) in Atera under a specified customer.
    
    If the contact already exists but is unassigned (CustomerID: 1), updates and migrates
    the contact to the new CustomerID.
    
    Args:
        customer_id: The CustomerID to assign the contact to.
        firstname: Contact's first name.
        lastname: Contact's last name.
        email: Contact's email address.
        job_title: Optional job title.
        phone: Optional office phone number.
        mobile_phone: Optional mobile phone number.
        
    Returns:
        str: A summary message of the contact creation/migration result.
    """
    api_key, base_url = load_atera_credentials()
    if not api_key:
        return "Error: Atera API credentials not configured in environment."

    async def _run():
        payload = {
            "Firstname": firstname,
            "Lastname": lastname,
            "Email": email,
            "CustomerID": customer_id,
            "IsContactPerson": False,
            "InIgnoreMode": False
        }
        if job_title:
            payload["JobTitle"] = job_title
        if phone:
            payload["Phone"] = phone
        if mobile_phone:
            payload["MobilePhone"] = mobile_phone

        async with AteraClient(api_key, base_url) as client:
            try:
                # 1. Check if contact already exists
                page = 1
                existing_contact = None
                while True:
                    data = await client._request("GET", "contacts", params={"page": page, "itemsInPage": 50})
                    if not data:
                        break
                    items = data.get("items", [])
                    for c in items:
                        if c.get("Email", "").strip().lower() == email.strip().lower() and not c.get("Archived", False):
                            existing_contact = c
                            break
                    if existing_contact or len(items) < 50:
                        break
                    page += 1

                if existing_contact:
                    cid = existing_contact.get("EndUserID")
                    current_customer_id = existing_contact.get("CustomerID")
                    
                    if current_customer_id == customer_id:
                        return f"Contact '{firstname} {lastname}' ({email}) already exists under CustomerID: {customer_id} (EndUserID: {cid})."
                    
                    # Migrate existing contact to the new customer
                    payload["CustomerID"] = customer_id
                    await client._request("PUT", f"contacts/{cid}", json_payload=payload)
                    return f"Contact '{firstname} {lastname}' ({email}) already existed and has been successfully migrated to CustomerID: {customer_id} (EndUserID: {cid})."

                # 2. Create contact if it does not exist
                res = await client._request("POST", "contacts", json_payload=payload)
                if isinstance(res, dict) and "ActionID" in res:
                    return f"Successfully created contact '{firstname} {lastname}' ({email}) under CustomerID: {customer_id} (EndUserID: {res['ActionID']})."
                return f"Successfully created contact '{firstname} {lastname}' ({email}) under CustomerID: {customer_id}."
            except Exception as e:
                return f"Error creating/migrating contact: {e}"

    return asyncio.run(_run())

def create_atera_ticket(
    title: str, 
    description: str, 
    customer_id: int, 
    priority: str = "Low", 
    contact_id: Optional[int] = None,
    status: str = "Open",
    group_id: Optional[int] = None
) -> str:
    """Logs a new support ticket in Atera.
    
    Args:
        title: The subject/title of the ticket.
        description: Detailed description of the issue/task.
        customer_id: The CustomerID the ticket belongs to.
        priority: Priority level ('Low', 'Medium', 'High', 'Critical').
        contact_id: Optional contact (EndUserID) to associate with the ticket.
        status: Status level (e.g. 'Open', 'Pending', 'Closed', 'Resolved').
        group_id: Optional technician group ID (e.g. 7 for Engineering).
        
    Returns:
        str: A summary message including the new TicketID.
    """
    import os
    api_key, base_url = load_atera_credentials()
    if not api_key:
        return "Error: Atera API credentials not configured in environment."

    async def _run():
        # Fallback to engineering group ID if not specified but needed
        resolved_group_id = group_id
        if not resolved_group_id:
            eng_env = os.environ.get("ATERA_ENGINEERING_GROUP_ID")
            if eng_env and eng_env.strip():
                try:
                    resolved_group_id = int(eng_env)
                except Exception:
                    pass
            if not resolved_group_id:
                resolved_group_id = 7 # Default Engineering

        async with AteraClient(api_key, base_url) as client:
            try:
                # 1. Create ticket
                res = await client.create_ticket(title, description, customer_id, priority)
                if res:
                    # 2. Add description as a public comment (so it shows up as first comment)
                    await client.add_ticket_comment(res, description, is_internal=False)
                    
                    # 3. Update the fields (contact, status, group)
                    update_fields = {}
                    if contact_id:
                        update_fields["EndUserID"] = contact_id
                    if status:
                        update_fields["TicketStatus"] = status
                    if resolved_group_id:
                        update_fields["TechnicianGroupID"] = resolved_group_id
                        
                    if update_fields:
                        await client.update_ticket_fields(res, update_fields)
                        
                    return f"Successfully created ticket '{title}' under CustomerID: {customer_id} (TicketID: {res}) with status: {status} and assigned to group: {resolved_group_id}."
                return f"Failed to retrieve TicketID after creation."
            except Exception as e:
                return f"Error creating ticket: {e}"

    return asyncio.run(_run())

